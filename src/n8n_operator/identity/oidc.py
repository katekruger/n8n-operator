"""OIDC bearer-token validation (ADR-014). The only module that speaks to an identity
provider — the same "one module owns the vendor boundary" shape ``n8n/client.py``
already has for n8n.

Pure validation only: signature, issuer, audience, algorithm, expiry/not-before/issued-
at (±60s clock skew, applied identically to all three — PyJWT's own ``leeway``), and
JWKS caching/rotation. This module never touches ``storage/`` — JIT principal
provisioning and the disabled-principal check are ``core/``'s job, composed with this
module's output at the one place both are allowed to be imported together
(``mcp/server.py``, the composition root — ARCHITECTURE.md section 2.1).

**Uniform failure.** Every validation failure — bad signature, wrong issuer, wrong
audience, forbidden algorithm, expired, not-yet-valid, unknown ``kid`` — collapses to
the same outcome: :meth:`OidcVerifier.verify` returns ``None``. Nothing here
distinguishes *why* a token failed, the same anti-oracle discipline ADR-002 already
applies to ``WORKFLOW_NOT_FOUND`` (ADR-014's own stated requirement: "An invalid token
... is INVALID_TOKEN, uniformly").

Phase 10 (v2) stage 02.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

__all__ = [
    "ALLOWED_ALGORITHMS",
    "DISPLAY_CLAIMS",
    "OidcVerifier",
    "ValidatedToken",
]

# Asymmetric algorithms only. Never "none" (an unsigned token), never an HMAC (HS*)
# algorithm — a caller who discovered any HMAC-signed token accepted here could forge
# tokens using the *public* JWKS key as the HMAC secret, the "alg confusion" attack
# this allowlist exists specifically to close.
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)

# Claims safe to carry into a display name / whoami result — never a claim that could be
# credential-shaped or carry authorization data of its own (e.g. no raw `scope`, no
# provider-internal role claims — those are exactly what ADR-015's own role grants
# exist to replace, not import from the token).
DISPLAY_CLAIMS = frozenset({"name", "email", "preferred_username"})


class _OidcValidationError(Exception):
    """Internal only — every raise site is caught by :meth:`OidcVerifier.verify` and
    turned into ``None``. Exists so the validation steps below can each fail fast via a
    normal exception instead of a chain of nested conditionals; it must never cross the
    module boundary, which is why :meth:`verify` is the only public entry point."""


@dataclass(frozen=True)
class ValidatedToken:
    """The result of a successful validation — exactly what ADR-014 says identity is:
    the pair, plus a few display-only claims. Never the raw token, never a raw claim
    outside :data:`DISPLAY_CLAIMS`."""

    issuer: str
    subject: str
    display_claims: dict[str, str] = field(default_factory=dict)


class OidcVerifier:
    """Validates bearer JWTs against one configured issuer/audience.

    JWKS discovery and fetch use a plain synchronous ``httpx.Client`` — consistent with
    every other outbound HTTP call in this codebase (``n8n/client.py``); the async
    ``TokenVerifier`` wrapper that plugs this into the MCP SDK (``mcp/server.py``)
    offloads it to a thread so a rare cache-miss fetch never blocks the event loop.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        http_client: httpx.Client | None = None,
        clock_skew_seconds: int = 60,
        jwks_refetch_min_interval_seconds: float = 60.0,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._http = http_client or httpx.Client(timeout=10.0)
        self._clock_skew_seconds = clock_skew_seconds
        self._refetch_min_interval = jwks_refetch_min_interval_seconds
        self._lock = threading.Lock()
        self._jwks_uri: str | None = jwks_uri
        self._keys_by_kid: dict[str, jwt.PyJWK] = {}
        self._last_failed_refetch_monotonic: float | None = None

    def _discover_jwks_uri(self) -> str:
        if self._jwks_uri is not None:
            return self._jwks_uri
        discovery_url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
        response = self._http.get(discovery_url)
        response.raise_for_status()
        document = response.json()
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise _OidcValidationError("discovery document has no jwks_uri")
        self._jwks_uri = jwks_uri
        return jwks_uri

    def _fetch_jwks(self) -> None:
        jwks_uri = self._discover_jwks_uri()
        response = self._http.get(jwks_uri)
        response.raise_for_status()
        document = response.json()
        keys: dict[str, jwt.PyJWK] = {}
        for jwk_data in document.get("keys", []):
            kid = jwk_data.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK.from_dict(jwk_data)
            except jwt.PyJWTError:
                # A key this process cannot use — an unsupported kty/crv
                # (InvalidKeyError), or a key PyJWT can parse but can't map to a JWS
                # algorithm (PyJWKError, e.g. a `use: "enc"` encryption key, which a
                # real-world realm publishes alongside its signing key by default) —
                # is skipped, not fatal. The JWKS document legitimately carries keys
                # for purposes other than the one this verifier needs; one unusable
                # entry must never take down every other key in the same document.
                # Caught this broadly (PyJWTError, PyJWK.from_dict's documented
                # exception base) deliberately, after a real Keycloak realm's default
                # JWKS — which always includes an RSA-OAEP encryption key next to the
                # RS256 signing key — proved InvalidKeyError alone misses PyJWKError,
                # PyJWK.from_dict's actual "can't find an algorithm for this key"
                # exception, silently breaking every subsequent verification.
                continue
        self._keys_by_kid = keys

    def _get_key(self, kid: str) -> jwt.PyJWK:
        """Fetch once and cache by ``kid``. A miss triggers exactly one re-fetch — even
        immediately after a prior *successful* fetch, since that one proved nothing
        about whether *this* ``kid`` exists. Only a re-fetch that itself still doesn't
        find the requested ``kid`` starts the rate-limit clock, so a genuine rotation
        (a brand-new ``kid`` the IdP just started publishing) is never mistaken for a
        forged-``kid`` probe and always gets its own real chance to be found
        (ADR-014 section 2)."""
        with self._lock:
            if kid in self._keys_by_kid:
                return self._keys_by_kid[kid]

            now = time.monotonic()
            if (
                self._last_failed_refetch_monotonic is not None
                and (now - self._last_failed_refetch_monotonic) < self._refetch_min_interval
            ):
                raise _OidcValidationError(f"unknown kid {kid!r}; refetch rate-limited")

            self._fetch_jwks()

            if kid not in self._keys_by_kid:
                self._last_failed_refetch_monotonic = time.monotonic()
                raise _OidcValidationError(f"unknown kid {kid!r} after refetch")

            self._last_failed_refetch_monotonic = None
            return self._keys_by_kid[kid]

    def _validate(self, token: str) -> ValidatedToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise _OidcValidationError("malformed token header") from exc

        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise _OidcValidationError(f"algorithm {algorithm!r} is not permitted")

        kid = header.get("kid")
        if not kid:
            raise _OidcValidationError("token header has no kid")

        try:
            key = self._get_key(kid)
        except (httpx.HTTPError, ValueError) as exc:
            raise _OidcValidationError("could not resolve signing key") from exc

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._clock_skew_seconds,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise _OidcValidationError("token failed validation") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise _OidcValidationError("token has no usable sub claim")

        display_claims = {
            name: str(claims[name]) for name in DISPLAY_CLAIMS if isinstance(claims.get(name), str)
        }
        return ValidatedToken(issuer=self._issuer, subject=subject, display_claims=display_claims)

    def verify(self, token: str) -> ValidatedToken | None:
        """The one public entry point. Never raises — every failure, expected or not,
        becomes ``None`` (see the module docstring's uniform-failure discipline)."""
        try:
            return self._validate(token)
        except _OidcValidationError:
            return None
        except Exception:
            return None
