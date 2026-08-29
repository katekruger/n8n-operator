"""Constructs the ``MCPServer`` and registers tools and resources.

The same tool set is registered regardless of transport, so the stdio and Streamable
HTTP surfaces are provably identical (AC-23) — :func:`build_server` is the one place
either transport (``mcp/transports.py``) gets a server from, and it always registers
the same twelve ``mcp/tools.py`` tools and the same two ``mcp/resources.py`` resources.
``caller_is_local`` is the one thing that legitimately differs per transport (stdio is
always local; a Streamable HTTP bind is local only when it is loopback) — it changes
one tool's result shaping (``prepare_operation``'s ``approval_url``, invariant I12), not
which tools or schemas exist, so it is a parameter here rather than baked in.

This module is the **composition root**: the one place ``n8n.client.N8nClient``,
``n8n.preflight.N8nPreflight``, ``n8n.health.N8nHealth``, and ``n8n.dispatch.N8nDispatch``
are constructed and wired into ``core.service`` through
``core.service.PreflightPort``/``HealthPort``/``DispatchPort``. No tool handler in
``mcp/tools.py`` touches ``n8n/`` directly (ARCHITECTURE.md section 2.1); the wiring that
makes that possible happens exactly once, here.

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hmac
from typing import Any

import anyio.to_thread
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.server import MCPServer
from pydantic import AnyHttpUrl
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import Settings, resolve_secret_reference
from n8n_operator.core.identity import ensure_dev_principal, resolve_user_principal
from n8n_operator.core.models import (
    DispatchOutcome,
    HealthCheckResult,
    PreflightCheck,
    PreflightResult,
    WorkflowContract,
)
from n8n_operator.identity.oidc import OidcVerifier
from n8n_operator.logging_setup import register_secret
from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.dispatch import N8nDispatch
from n8n_operator.n8n.health import N8nHealth
from n8n_operator.n8n.preflight import N8nPreflight
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import session_scope

__all__ = ["build_server"]


class _PreflightAdapter:
    """Bridges ``n8n.preflight.N8nPreflight``'s local, duck-typed result dataclasses
    onto the real ``core.models.PreflightResult``/``PreflightCheck`` this composition
    root — and only this composition root — is allowed to import both sides to do
    (``n8n/`` may not import ``core/``; this module is neither)."""

    def __init__(self, impl: N8nPreflight) -> None:
        self._impl = impl

    def check(self, workflow: WorkflowContract) -> PreflightResult:
        raw = self._impl.check(workflow)
        return PreflightResult(
            ready=raw.ready,
            checks=[
                PreflightCheck(check=c.check, status=c.status, code=c.code, detail=c.detail)  # type: ignore[arg-type]
                for c in raw.checks
            ],
            checked_at=raw.checked_at,
        )


class _HealthAdapter:
    """As :class:`_PreflightAdapter`, for ``n8n.health.N8nHealth``."""

    def __init__(self, impl: N8nHealth) -> None:
        self._impl = impl

    def check(self) -> HealthCheckResult:
        raw = self._impl.check()
        return HealthCheckResult(
            reachable=raw.reachable,
            n8n_version=raw.n8n_version,
            latency_ms=raw.latency_ms,
            reason=raw.reason,
            checked_at=raw.checked_at,
        )


class _DispatchAdapter:
    """As :class:`_PreflightAdapter`/:class:`_HealthAdapter`, for ``n8n.dispatch.N8nDispatch``.

    ``fetch_node_trace`` needs no conversion — ``n8n.client.get_execution_node_trace``
    already returns the plain, allowlist-shaped ``dict[str, Any] | None`` that
    ``core.service.DispatchPort`` expects verbatim.
    """

    def __init__(self, impl: N8nDispatch) -> None:
        self._impl = impl

    def dispatch(
        self, workflow: WorkflowContract, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        raw = self._impl.dispatch(workflow, arguments, timeout_seconds=timeout_seconds)
        return DispatchOutcome(
            kind=raw.kind,
            http_status=raw.http_status,
            result=raw.result,
            execution_id=raw.execution_id,
            correlation_available=raw.correlation_available,
        )

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        return self._impl.fetch_node_trace(execution_id)


class _OperatorTokenVerifier(TokenVerifier):
    """Bridges ``identity/oidc.py`` (pure JWT/JWKS validation) and ``core/identity.py``
    (JIT provisioning, the disabled-principal check) into the one thing the MCP SDK's
    ``BearerAuthBackend`` needs — the same "two decoupled layers meet only at the
    composition root" shape :class:`_PreflightAdapter` and friends already establish
    for n8n I/O (``identity/`` may not import ``storage/`` or ``core/``, and neither of
    those may import the other's sibling capability package — ARCHITECTURE.md
    section 2.1).

    Also the service-principal credential path (ADR-013 section 3): a service
    principal has no OIDC identity, so a presented token that is not a valid JWT is
    additionally checked against every enabled service principal's resolved
    ``credential_ref`` — constant-time, the same discipline v1's single static bearer
    token already used.

    Every sync step (JWT/JWKS validation, the database round trip) runs in a worker
    thread via ``anyio.to_thread.run_sync`` so a rare JWKS cache-miss fetch, or a
    momentarily slow database, never blocks the event loop other requests share.
    """

    def __init__(self, *, oidc: OidcVerifier, session_factory: sessionmaker[Session]) -> None:
        self._oidc = oidc
        self._session_factory = session_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        validated = await anyio.to_thread.run_sync(self._oidc.verify, token)
        if validated is not None:
            principal_id = await anyio.to_thread.run_sync(self._resolve_user, validated)
            if principal_id is None:
                return None
            return AccessToken(
                token=token,
                client_id=principal_id,
                scopes=[],
                subject=validated.subject,
                claims={"iss": validated.issuer, "principal_id": principal_id, "kind": "user"},
            )

        service_principal_id = await anyio.to_thread.run_sync(self._resolve_service, token)
        if service_principal_id is not None:
            return AccessToken(
                token=token,
                client_id=service_principal_id,
                scopes=[],
                subject=None,
                claims={"principal_id": service_principal_id, "kind": "service"},
            )
        return None

    def _resolve_user(self, validated: Any) -> str | None:
        with session_scope(self._session_factory) as session:
            principal = resolve_user_principal(
                session,
                issuer=validated.issuer,
                subject=validated.subject,
                display_name_hint=(
                    validated.display_claims.get("name")
                    or validated.display_claims.get("email")
                    or validated.display_claims.get("preferred_username")
                ),
            )
            return principal.id if principal is not None else None

    def _resolve_service(self, token: str) -> str | None:
        if not token:
            return None
        with session_scope(self._session_factory) as session:
            for candidate in PrincipalRepository(session).list_service_principals(
                include_disabled=False
            ):
                if not candidate.credential_ref:
                    continue
                try:
                    resolved = resolve_secret_reference(candidate.credential_ref)
                except ValueError:
                    continue
                # Every resolved service-credential value is registered for log
                # scrubbing the moment it is read — not only on a match — the same
                # discipline `serve.py` already applies to `n8n_api_key`/
                # `http_bearer_token`, extended here since this value is looked up
                # live, per candidate, per request, rather than once at startup.
                register_secret(resolved)
                if hmac.compare_digest(resolved.encode(), token.encode()):
                    return candidate.id
        return None


def build_server(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    caller_is_local: bool,
    is_stdio: bool = False,
) -> MCPServer[Any]:
    """Build one fully-wired ``MCPServer``: the real n8n client and its preflight and
    health adapters, the v1 tools (plus ``whoami`` when ``settings.enable_v2``), and
    the two v1 resources.

    v2 identity (stage 02, ADR-014): stdio (``is_stdio=True``) *always* attributes the
    caller to one fixed, visibly-labeled service principal, idempotently ensured to
    exist right here at startup rather than requiring a separate seeding step — no
    OIDC session can flow over stdio, protocol-level, regardless of
    ``identity_mode`` (ADR-014 section 5). Streamable HTTP follows
    ``settings.identity_mode`` exactly as configured: ``"dev"`` uses that same fixed
    principal (``config.py``'s own validator already refuses this combination on a
    non-loopback bind); ``"oidc"`` wires a real ``TokenVerifier`` into the SDK, which
    per-request resolves the actual caller (``mcp/tools.py``'s
    ``_resolve_principal_id`` reads it back out of the SDK's own auth contextvar) —
    including over a loopback bind, so an operator can deliberately exercise real OIDC
    locally before a non-loopback rollout.
    """
    client = N8nClient(
        base_url=str(settings.n8n_base_url),
        api_key=settings.n8n_api_key.get_secret_value(),
        connect_timeout_seconds=float(settings.request_timeout_seconds),
    )

    principal_id = "local"
    token_verifier: TokenVerifier | None = None
    auth_settings: AuthSettings | None = None

    if settings.enable_v2:
        use_dev_identity = is_stdio or settings.identity_mode == "dev"
        if use_dev_identity:
            with session_scope(session_factory) as session:
                dev_principal = ensure_dev_principal(
                    session, principal_id=settings.dev_principal_id
                )
                principal_id = dev_principal.id
        else:
            assert settings.oidc_issuer_url is not None and settings.oidc_audience is not None
            oidc = OidcVerifier(
                issuer=str(settings.oidc_issuer_url).rstrip("/"),
                audience=settings.oidc_audience,
                jwks_uri=str(settings.oidc_jwks_uri) if settings.oidc_jwks_uri else None,
            )
            token_verifier = _OperatorTokenVerifier(oidc=oidc, session_factory=session_factory)
            auth_settings = AuthSettings(
                issuer_url=AnyHttpUrl(str(settings.oidc_issuer_url)),
                resource_server_url=(
                    AnyHttpUrl(str(settings.oidc_resource_server_url))
                    if settings.oidc_resource_server_url
                    else None
                ),
            )

    deps = ToolDeps(
        session_factory=session_factory,
        preflight=_PreflightAdapter(N8nPreflight(client)),
        health=_HealthAdapter(N8nHealth(client)),
        dispatch=_DispatchAdapter(N8nDispatch(client)),
        server_max_argument_bytes=settings.max_argument_bytes,
        principal_id=principal_id,
        caller_is_local=caller_is_local,
        approval_base_url=f"http://{settings.approval_bind}",
        known_secrets=client.known_secrets(),
        enable_v2=settings.enable_v2,
    )
    server: MCPServer[Any] = MCPServer(
        "n8n-operator",
        title="n8n Operator",
        description=(
            "A governed control plane for discovering, validating, executing, and "
            "debugging approved n8n workflows."
        ),
        tools=build_tools(deps),
        token_verifier=token_verifier,
        auth=auth_settings,
    )
    register_resources(server, deps)
    return server
