"""``mcp.server._OperatorTokenVerifier`` — the composition-root bridge between
``identity/oidc.py`` (pure JWT validation) and ``core/identity.py``/``storage/``
(JIT provisioning, the disabled-principal check, service-principal credential
matching). Exercised directly against a real SQLite database and a real RSA-signed
JWT, since this is where every one of stage 02's required negative cases actually
resolves to a database-backed decision.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator import logging_setup
from n8n_operator.core.identity import build_whoami
from n8n_operator.identity.oidc import OidcVerifier
from n8n_operator.mcp.server import _OperatorTokenVerifier
from n8n_operator.storage.repository import (
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

ISSUER = "https://idp.example.com"
AUDIENCE = "n8n-operator"
KID = "key-1"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_public(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    algorithm = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    jwk: dict[str, Any] = json.loads(algorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _sign(
    private_key: rsa.RSAPrivateKey, *, sub: str, extra_claims: dict[str, Any] | None = None
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "iat": now,
        "exp": now + 3600,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture(scope="module")
def keypair() -> rsa.RSAPrivateKey:
    return _rsa_key()


@pytest.fixture
def verifier(
    keypair: rsa.RSAPrivateKey, session_factory: sessionmaker[Session]
) -> _OperatorTokenVerifier:
    jwks = {"keys": [_jwk_public(keypair, KID)]}

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(_handle))
    oidc = OidcVerifier(issuer=ISSUER, audience=AUDIENCE, http_client=client)
    return _OperatorTokenVerifier(oidc=oidc, session_factory=session_factory)


@pytest.mark.integration
async def test_a_valid_token_jit_provisions_a_new_user_principal(
    verifier: _OperatorTokenVerifier,
    keypair: rsa.RSAPrivateKey,
    session_factory: sessionmaker[Session],
) -> None:
    token = _sign(keypair, sub="alice", extra_claims={"name": "Alice A"})
    access_token = await verifier.verify_token(token)
    assert access_token is not None
    assert access_token.claims is not None
    assert access_token.claims["kind"] == "user"
    principal_id = access_token.claims["principal_id"]

    with session_factory() as session:
        principal = PrincipalRepository(session).get(principal_id)
        assert principal is not None
        assert principal.kind == "user"
        assert principal.external_issuer == ISSUER
        assert principal.external_subject == "alice"
        assert principal.display_name == "Alice A"


@pytest.mark.integration
async def test_duplicate_subject_mapping_reuses_the_same_principal(
    verifier: _OperatorTokenVerifier, keypair: rsa.RSAPrivateKey
) -> None:
    """The exact negative case named by stage 02: authenticating twice as the same
    ``(iss, sub)`` must never create a second principal row (ADR-014's identity
    anchor, enforced by ``uq_principals_external_identity`` — this proves the
    *resolution path* respects it, not only the constraint)."""
    first = await verifier.verify_token(_sign(keypair, sub="bob"))
    second = await verifier.verify_token(_sign(keypair, sub="bob"))
    assert first is not None
    assert second is not None
    assert first.claims is not None
    assert second.claims is not None
    assert first.claims["principal_id"] == second.claims["principal_id"]


@pytest.mark.integration
async def test_a_disabled_principal_is_rejected_even_with_a_valid_token(
    verifier: _OperatorTokenVerifier,
    keypair: rsa.RSAPrivateKey,
    session_factory: sessionmaker[Session],
) -> None:
    token = _sign(keypair, sub="carol")
    first = await verifier.verify_token(token)
    assert first is not None
    assert first.claims is not None
    principal_id = first.claims["principal_id"]

    with session_scope(session_factory) as session:
        PrincipalRepository(session).disable(principal_id)

    # The identical, still cryptographically valid token now fails — disabled status
    # is re-checked live on every call, never cached (ADR-014 section 4).
    second = await verifier.verify_token(token)
    assert second is None


@pytest.mark.integration
async def test_a_removed_membership_leaves_whoami_empty_but_the_principal_still_authenticates(
    verifier: _OperatorTokenVerifier,
    keypair: rsa.RSAPrivateKey,
    session_factory: sessionmaker[Session],
) -> None:
    """Removing a membership is not the same as disabling a principal — the token
    keeps authenticating (identity is still valid), but a *fresh* read of
    memberships (``whoami``'s own path, not this verifier's) reflects the removal
    immediately, never a cached prior state."""
    token = _sign(keypair, sub="dave")
    result = await verifier.verify_token(token)
    assert result is not None
    assert result.claims is not None
    principal_id = result.claims["principal_id"]

    with session_scope(session_factory) as session:
        org = OrganizationRepository(session).create(name="Acme")
        membership = OrganizationMembershipRepository(session).create(
            principal_id=principal_id, organization_id=org.id, roles=["viewer"]
        )

    with session_factory() as session:
        principal = PrincipalRepository(session).get(principal_id)
        assert principal is not None
        who = build_whoami(session, principal)
        assert len(who.organizations) == 1

    with session_scope(session_factory) as session:
        OrganizationMembershipRepository(session).remove(membership.id)

    # The token itself still authenticates...
    assert await verifier.verify_token(token) is not None
    # ...but a fresh whoami read no longer shows the removed organization.
    with session_factory() as session:
        principal = PrincipalRepository(session).get(principal_id)
        assert principal is not None
        who = build_whoami(session, principal)
        assert who.organizations == []


@pytest.mark.integration
async def test_service_principal_authenticates_by_credential_never_by_jwt(
    verifier: _OperatorTokenVerifier,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "a service principal using an interactive-only path" — the negative case
    stage 02 names: a service principal has no OIDC identity at all
    (``external_subject``/``external_issuer`` are both ``NULL``), so no JWT, however
    validly signed, can ever resolve to one. It authenticates only by presenting its
    configured credential directly as the bearer token."""
    monkeypatch.setenv("N8N_OPERATOR_TEST_SVC_TOKEN", "the-service-credential-0000000000")
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="service",
            display_name="CI bot",
            credential_ref="env:N8N_OPERATOR_TEST_SVC_TOKEN",
        )
        service_principal_id = principal.id

    result = await verifier.verify_token("the-service-credential-0000000000")
    assert result is not None
    assert result.claims is not None
    assert result.claims["principal_id"] == service_principal_id
    assert result.claims["kind"] == "service"


@pytest.mark.integration
async def test_wrong_service_credential_is_rejected(
    verifier: _OperatorTokenVerifier,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_OPERATOR_TEST_SVC_TOKEN", "the-real-credential-0000000000")
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(
            kind="service", display_name="CI bot", credential_ref="env:N8N_OPERATOR_TEST_SVC_TOKEN"
        )

    assert await verifier.verify_token("not-the-right-credential") is None
    assert await verifier.verify_token("") is None


@pytest.mark.integration
async def test_a_disabled_service_principals_credential_is_rejected(
    verifier: _OperatorTokenVerifier,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_OPERATOR_TEST_SVC_TOKEN", "a-disabled-credential-0000000000")
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="service", display_name="CI bot", credential_ref="env:N8N_OPERATOR_TEST_SVC_TOKEN"
        )
        PrincipalRepository(session).disable(principal.id)

    assert await verifier.verify_token("a-disabled-credential-0000000000") is None


@pytest.mark.integration
async def test_an_empty_or_unrelated_token_matches_neither_path(
    verifier: _OperatorTokenVerifier,
) -> None:
    assert await verifier.verify_token("garbage-not-a-jwt-or-a-credential") is None
    assert await verifier.verify_token("") is None


@pytest.mark.integration
async def test_resolving_a_service_credential_registers_it_for_log_scrubbing(
    verifier: _OperatorTokenVerifier,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stage 02 negative case "log/result secret leaks": a service principal's
    ``credential_ref`` is resolved live, per request (never cached, never stored) —
    the resolved value must be registered with ``logging_setup.register_secret`` the
    moment it is read, the same discipline already applied to ``n8n_api_key`` and
    ``http_bearer_token`` at startup, so it is scrubbed from any log line it might
    otherwise appear in even though it was never a static, startup-known value."""
    logging_setup._reset_registered_secrets_for_tests()
    monkeypatch.setenv("N8N_OPERATOR_TEST_SVC_TOKEN", "a-scrub-worthy-credential-00000")
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(
            kind="service",
            display_name="CI bot",
            credential_ref="env:N8N_OPERATOR_TEST_SVC_TOKEN",
        )

    try:
        # A failed match still resolves (and must still register) the candidate's
        # credential — scrubbing must not depend on this request happening to be the
        # one that used it.
        assert await verifier.verify_token("not-the-right-credential") is None
        assert "a-scrub-worthy-credential-00000" in logging_setup._known_secrets
    finally:
        logging_setup._reset_registered_secrets_for_tests()
