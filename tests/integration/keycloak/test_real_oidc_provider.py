"""End-to-end proof against a real, running Keycloak instance (``docker/keycloak-test/``).

``tests/unit/test_identity_oidc.py``, ``tests/integration/test_operator_token_verifier.py``,
and ``tests/integration/test_mcp_oidc_transport.py`` exhaustively cover the *validation
logic* — every accept/reject branch — against hand-signed RSA JWTs and a mocked HTTP
transport (``httpx.MockTransport``) standing in for a real IdP's discovery/JWKS/token
endpoints. None of them ever talk to an actual OIDC identity provider over real HTTP.

This module closes that specific gap and no other: it proves the *real* discovery
document, the *real* JWKS endpoint, and *real* signed tokens from an actual running
Keycloak instance work end-to-end through this codebase's ``OidcVerifier`` and
``_OperatorTokenVerifier`` — not a second pass over every negative-case validation
branch, which the unit-level suite already owns.

Skips (not errors) whenever ``N8N_OPERATOR_TEST_KEYCLOAK_URL`` is unset — see
``conftest.py`` in this package — exactly mirroring
``tests/integration/postgres/``'s opt-in pattern, so a checkout without the Keycloak
container running is unaffected.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.identity.oidc import OidcVerifier
from n8n_operator.mcp.server import _OperatorTokenVerifier

from .conftest import CLIENT_ID, TEST_USERNAME, password_grant

pytestmark = pytest.mark.keycloak


def test_discovers_the_real_issuer_and_verifies_a_real_signed_token(
    keycloak_issuer_url: str, real_access_token: str
) -> None:
    """``OidcVerifier``, built with a real ``httpx.Client()`` and no mock transport,
    discovers ``jwks_uri`` from the real ``.well-known/openid-configuration`` document,
    fetches the real JWKS, and successfully verifies a token Keycloak actually signed.
    """
    with httpx.Client() as client:
        verifier = OidcVerifier(issuer=keycloak_issuer_url, audience=CLIENT_ID, http_client=client)
        validated = verifier.verify(real_access_token)

    assert validated is not None
    assert validated.issuer == keycloak_issuer_url
    assert validated.subject


def test_wrong_password_fails_at_keycloaks_own_token_endpoint(
    keycloak_token_endpoint: str,
) -> None:
    """A wrong-password grant is rejected by Keycloak itself — no token is ever minted,
    so OidcVerifier is never exercised at all. This is expected, not a test failure.
    """
    response = password_grant(
        keycloak_token_endpoint, username=TEST_USERNAME, password="definitely-not-alices-password"
    )

    assert response.status_code == 401
    body = response.json()
    assert "access_token" not in body
    assert body.get("error") == "invalid_grant"


def test_real_token_is_rejected_when_configured_for_a_different_audience(
    keycloak_issuer_url: str, real_access_token: str
) -> None:
    """A real Keycloak token, whose real `aud` claim is `n8n-operator` (via the realm's
    audience protocol mapper), is rejected by OidcVerifier's real audience check when
    the verifier is configured for a different audience.
    """
    with httpx.Client() as client:
        verifier = OidcVerifier(
            issuer=keycloak_issuer_url,
            audience="some-other-audience-entirely",
            http_client=client,
        )
        validated = verifier.verify(real_access_token)

    assert validated is None


async def test_operator_token_verifier_jit_provisions_a_real_principal_row(
    keycloak_issuer_url: str,
    real_access_token: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The same real token, run through ``_OperatorTokenVerifier`` (a real
    ``OidcVerifier`` plus a real SQLite ``session_factory``), JIT-provisions a real
    ``principals`` row keyed on ``(external_issuer, external_subject)``.
    """
    from n8n_operator.storage.repository import PrincipalRepository
    from n8n_operator.storage.session import session_scope

    with httpx.Client() as client:
        oidc_verifier = OidcVerifier(
            issuer=keycloak_issuer_url, audience=CLIENT_ID, http_client=client
        )
        operator_verifier = _OperatorTokenVerifier(
            oidc=oidc_verifier, session_factory=session_factory
        )
        result = await operator_verifier.verify_token(real_access_token)

    assert result is not None
    assert result.claims is not None
    principal_id = result.claims["principal_id"]

    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).get(principal_id)
        assert principal is not None
        assert principal.external_issuer == keycloak_issuer_url
        assert principal.external_subject
        assert principal.disabled_at is None
