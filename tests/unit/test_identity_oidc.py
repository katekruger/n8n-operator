"""``OidcVerifier`` against real RSA-signed JWTs and a mocked IdP (discovery + JWKS) —
every negative case ADR-014/stage 02 names, each proven to collapse to the same
``None`` outcome (the anti-oracle discipline the module docstring states).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from n8n_operator.identity.oidc import OidcVerifier

ISSUER = "https://idp.example.com"
AUDIENCE = "n8n-operator"
KID = "key-1"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_public(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    algorithm = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    jwk_json = algorithm.to_jwk(private_key.public_key())
    import json

    jwk: dict[str, Any] = json.loads(jwk_json)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _sign(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = KID,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    algorithm: str = "RS256",
    sub: str | None = "user-123",
    exp_offset: int = 3600,
    iat_offset: int = 0,
    nbf_offset: int | None = None,
    extra_claims: dict[str, Any] | None = None,
    extra_headers: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
    }
    if sub is not None:
        claims["sub"] = sub
    if nbf_offset is not None:
        claims["nbf"] = now + nbf_offset
    if extra_claims:
        claims.update(extra_claims)
    headers = {"kid": kid}
    if extra_headers:
        headers.update(extra_headers)
    return jwt.encode(claims, private_key, algorithm=algorithm, headers=headers)


@pytest.fixture(scope="module")
def keypair() -> rsa.RSAPrivateKey:
    return _rsa_key()


@pytest.fixture(scope="module")
def other_keypair() -> rsa.RSAPrivateKey:
    """A second key never published in the JWKS — signs tokens that must always fail
    (a forged `kid` pointing at a real ID, but a signature that key cannot verify)."""
    return _rsa_key()


def _mock_transport(
    keypair: rsa.RSAPrivateKey, *, kid: str = KID, jwks_call_count: list[int] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    jwks = {"keys": [_jwk_public(keypair, kid)]}

    def _handle(request: httpx.Request) -> httpx.Response:
        if jwks_call_count is not None and request.url.path.endswith("jwks.json"):
            jwks_call_count[0] += 1
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return _handle


def _verifier(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> OidcVerifier:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OidcVerifier(issuer=ISSUER, audience=AUDIENCE, http_client=client, **kwargs)


@pytest.mark.unit
def test_a_valid_token_verifies(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair)
    result = verifier.verify(token)
    assert result is not None
    assert result.issuer == ISSUER
    assert result.subject == "user-123"


@pytest.mark.unit
def test_a_jwks_document_with_an_unusable_encryption_key_still_verifies(
    keypair: rsa.RSAPrivateKey,
) -> None:
    """Regression: a real-world JWKS document (proven against a real Keycloak realm,
    ``docker/keycloak-test/``) publishes an ``RSA-OAEP``/``use: "enc"`` encryption key
    alongside the ``RS256`` signing key by default — entirely spec-compliant, and
    something a mocked-only test suite never happened to construct. ``PyJWK.from_dict``
    raises ``PyJWKError`` (not ``InvalidKeyError``) for that entry — a sibling
    exception the fetch loop originally didn't catch, so one unusable key silently
    broke every subsequent verification (swallowed by :meth:`OidcVerifier.verify`'s
    uniform-failure discipline into the same ``None`` as any other rejection, with no
    way to tell the two apart short of exactly this kind of end-to-end test)."""
    encryption_key = _rsa_key()
    signing_jwk = _jwk_public(keypair, KID)
    encryption_jwk: dict[str, Any] = {
        "kid": "enc-key-1",
        "kty": "RSA",
        "alg": "RSA-OAEP",
        "use": "enc",
        "n": _jwk_public(encryption_key, "enc-key-1")["n"],
        "e": _jwk_public(encryption_key, "enc-key-1")["e"],
    }
    jwks = {"keys": [encryption_jwk, signing_jwk]}

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    verifier = _verifier(_handle)
    result = verifier.verify(_sign(keypair))
    assert result is not None
    assert result.subject == "user-123"


@pytest.mark.unit
def test_display_claims_pass_through_but_nothing_else(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, extra_claims={"name": "Kate", "scope": "admin:all", "roles": ["admin"]})
    result = verifier.verify(token)
    assert result is not None
    assert result.display_claims == {"name": "Kate"}


@pytest.mark.unit
def test_wrong_issuer_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, issuer="https://attacker.example.com")
    assert verifier.verify(token) is None


@pytest.mark.unit
def test_wrong_audience_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, audience="some-other-service")
    assert verifier.verify(token) is None


@pytest.mark.unit
def test_forbidden_algorithm_none_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    now = int(time.time())
    # jwt.encode refuses alg="none" with a key; build the unsigned token by hand.
    import base64
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": KID}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-123", "iat": now, "exp": now + 3600}
        ).encode()
    ).rstrip(b"=")
    unsigned_token = (header + b"." + payload + b".").decode()
    assert verifier.verify(unsigned_token) is None


@pytest.mark.unit
def test_forbidden_algorithm_hs256_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    """The alg-confusion attack: signing with HS256 using the RSA public key's PEM as
    the HMAC secret must never be accepted just because *a* signature validates.

    PyJWT's own ``encode`` already refuses to build this token (it detects a PEM/SSH
    key passed as an HMAC secret and raises) — a real, independent defense this test
    doesn't get to rely on, since a verifier must reject the algorithm on the *decode*
    side regardless of whether every possible encoder would cooperate in producing the
    attack. The header/payload/signature are built by hand instead, bypassing that
    encode-side guard, to prove the allowlist check in ``_validate`` rejects the
    algorithm before ever attempting to resolve or use a key.
    """
    import base64
    import hashlib
    import hmac as hmac_module
    import json

    verifier = _verifier(_mock_transport(keypair))
    public_pem = keypair.public_key().public_bytes(
        encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())

    def _b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "kid": KID}).encode())
    payload = _b64(
        json.dumps(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-123", "iat": now, "exp": now + 3600}
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = _b64(hmac_module.new(public_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    assert verifier.verify(forged) is None


@pytest.mark.unit
def test_unsigned_token_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    assert verifier.verify("not.a.jwt") is None
    assert verifier.verify("") is None


@pytest.mark.unit
def test_expired_token_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, exp_offset=-3600)
    assert verifier.verify(token) is None


@pytest.mark.unit
def test_future_token_before_nbf_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, nbf_offset=3600)
    assert verifier.verify(token) is None


@pytest.mark.unit
def test_token_within_clock_skew_tolerance_is_accepted(keypair: rsa.RSAPrivateKey) -> None:
    """±60s is tolerated identically for exp/iat/nbf — a token 59s past its expiry (or
    59s before its nbf) is still valid; 61s is not."""
    verifier = _verifier(_mock_transport(keypair), clock_skew_seconds=60)
    just_expired = _sign(keypair, exp_offset=-59)
    just_before_nbf = _sign(keypair, nbf_offset=59)
    assert verifier.verify(just_expired) is not None
    assert verifier.verify(just_before_nbf) is not None

    just_too_expired = _sign(keypair, exp_offset=-61)
    just_too_early = _sign(keypair, nbf_offset=61)
    assert verifier.verify(just_too_expired) is None
    assert verifier.verify(just_too_early) is None


@pytest.mark.unit
def test_signature_from_an_unpublished_key_is_rejected(
    keypair: rsa.RSAPrivateKey, other_keypair: rsa.RSAPrivateKey
) -> None:
    """A token whose `kid` matches a real published key ID, but whose signature was
    produced by a *different* private key — the direct forgery case."""
    verifier = _verifier(_mock_transport(keypair))
    forged = _sign(other_keypair, kid=KID)
    assert verifier.verify(forged) is None


@pytest.mark.unit
def test_unknown_kid_triggers_exactly_one_refetch(keypair: rsa.RSAPrivateKey) -> None:
    calls = [0]
    verifier = _verifier(_mock_transport(keypair, jwks_call_count=calls))
    # Prime the cache with a first, valid call.
    assert verifier.verify(_sign(keypair)) is not None
    assert calls[0] == 1

    # An unknown kid triggers exactly one more fetch, which still doesn't find it.
    unknown = _sign(keypair, kid="never-published")
    assert verifier.verify(unknown) is None
    assert calls[0] == 2


@pytest.mark.unit
def test_unknown_kid_refetch_is_rate_limited(keypair: rsa.RSAPrivateKey) -> None:
    calls = [0]
    verifier = _verifier(
        _mock_transport(keypair, jwks_call_count=calls), jwks_refetch_min_interval_seconds=60.0
    )
    unknown = _sign(keypair, kid="never-published")
    assert verifier.verify(unknown) is None
    first_call_count = calls[0]
    assert first_call_count >= 1

    # A second, third, fourth attempt within the rate-limit window triggers no
    # additional fetch — bounded cost under a forged-kid probe (ADR-014 section 2).
    for _ in range(5):
        assert verifier.verify(unknown) is None
    assert calls[0] == first_call_count


@pytest.mark.unit
def test_jwks_rotation_is_picked_up_after_the_old_key_stops_working(
    keypair: rsa.RSAPrivateKey,
) -> None:
    """Simulates a real rotation: the IdP starts publishing a new key under a new
    `kid`; a token signed with it is unknown until the verifier re-fetches, then
    verifies — proving rotation actually works, not just that unknown-kid fails."""
    rotated_key = _rsa_key()
    new_kid = "key-2"
    jwks = {"keys": [_jwk_public(keypair, KID)]}

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(_handle))
    verifier = OidcVerifier(issuer=ISSUER, audience=AUDIENCE, http_client=client)

    assert verifier.verify(_sign(keypair)) is not None

    # The IdP rotates: JWKS now serves both keys (a real rotation keeps the old one
    # around briefly so still-valid old tokens keep working).
    jwks["keys"] = [_jwk_public(keypair, KID), _jwk_public(rotated_key, new_kid)]

    rotated_token = _sign(rotated_key, kid=new_kid)
    assert verifier.verify(rotated_token) is not None
    # The pre-rotation token, signed with the still-published old key, still verifies.
    assert verifier.verify(_sign(keypair)) is not None


@pytest.mark.unit
def test_no_sub_claim_is_rejected(keypair: rsa.RSAPrivateKey) -> None:
    verifier = _verifier(_mock_transport(keypair))
    token = _sign(keypair, sub=None)
    assert verifier.verify(token) is None


@pytest.mark.unit
def test_discovery_failure_is_rejected_not_raised(keypair: rsa.RSAPrivateKey) -> None:
    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(_handle))
    verifier = OidcVerifier(issuer=ISSUER, audience=AUDIENCE, http_client=client)
    assert verifier.verify(_sign(keypair)) is None


@pytest.mark.unit
def test_a_configured_jwks_uri_skips_discovery(keypair: rsa.RSAPrivateKey) -> None:
    discovery_calls = [0]

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            discovery_calls[0] += 1
            return httpx.Response(500)  # would fail verification if ever called
        if request.url.path == "/custom-jwks":
            return httpx.Response(200, json={"keys": [_jwk_public(keypair, KID)]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(_handle))
    verifier = OidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=f"{ISSUER}/custom-jwks", http_client=client
    )
    assert verifier.verify(_sign(keypair)) is not None
    assert discovery_calls[0] == 0
