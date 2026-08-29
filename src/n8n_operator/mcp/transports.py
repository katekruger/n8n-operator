"""stdio and Streamable HTTP transports.

stdio is the default: the parent process is the security boundary and no network
listener exists. Streamable HTTP binds ``127.0.0.1`` by default; a non-loopback bind
requires an Origin allowlist and, unless v2 OIDC identity is configured, a bearer
token, or startup fails (boundary B9, AC-20). The Origin check is DNS-rebinding
defense (threat T-34).

``config.Settings`` already refuses to *construct* on a non-loopback ``http_bind``
without a non-empty Origin allowlist and (absent OIDC) a bearer token (its own
``_validate_http_bind_guard``) — that is the authoritative "refuses to start"
enforcement B9 asks for. :class:`_TransportSecurityMiddleware` here is the second half:
*actual per-request* rejection of a missing/disallowed ``Origin`` header and, when
applicable, a missing/invalid static bearer token — which the installed MCP SDK's own
``TransportSecuritySettings`` does not fully provide (it treats an *absent* Origin
header as same-origin and lets it through — the wrong default for a listener that must
reject exactly that). It is applied only on a non-loopback bind; a loopback bind is
unreachable from anywhere but a local process to begin with. When v2 OIDC identity is
active, the static bearer-token half is skipped here — the SDK's own auth middleware
(wired in by ``mcp/server.py``'s ``token_verifier``) enforces a real per-request JWT
instead (ADR-014 section 1).

:class:`_CorrelationIdMiddleware` (phase 8) is the Streamable HTTP counterpart to the
CLI root callback's one-per-invocation correlation ID (``cli/main.py``): applied
unconditionally, it binds a fresh ID per HTTP request rather than per process, since one
long-running server process handles many requests.

Phases 5 and 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hmac

import anyio
import uvicorn
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from n8n_operator.config import Settings
from n8n_operator.logging_setup import correlation_scope
from n8n_operator.mcp.server import build_server

__all__ = ["serve_http", "serve_stdio"]


def _is_loopback_bind(bind: str) -> bool:
    host = bind.rsplit(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


class _TransportSecurityMiddleware:
    """Rejects a non-loopback Streamable HTTP request with a missing/disallowed
    ``Origin`` or (unless ``bearer_token`` is ``None``) a missing/invalid bearer
    ``Authorization`` header, before it reaches the MCP session machinery
    (boundary B9, AC-20).

    Constructed only when the bind is non-loopback, at which point ``config.Settings``
    already guarantees ``allowed_origins`` is non-empty — this class does not re-derive
    that guarantee, it enforces it per request. The Origin check always applies
    (DNS-rebinding defense, threat T-34, independent of how identity is proven); the
    static bearer-token check is skipped when ``bearer_token`` is ``None`` — v2 OIDC
    identity mode (``mcp/server.py``'s ``token_verifier``) *replaces* it with a real
    per-request bearer JWT enforced by the MCP SDK's own auth middleware instead
    (ADR-014 section 1), rather than layering a second, meaningless static check on
    top of it.
    """

    def __init__(
        self, app: ASGIApp, *, bearer_token: str | None, allowed_origins: tuple[str, ...]
    ) -> None:
        self._app = app
        self._expected_authorization = (
            f"Bearer {bearer_token}".encode() if bearer_token is not None else None
        )
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None or origin not in self._allowed_origins:
            await JSONResponse({"error": "origin not allowed"}, status_code=403)(
                scope, receive, send
            )
            return

        if self._expected_authorization is not None:
            authorization = headers.get("authorization", "").encode()
            if not hmac.compare_digest(authorization, self._expected_authorization):
                await JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)(
                    scope, receive, send
                )
                return

        await self._app(scope, receive, send)


class _CorrelationIdMiddleware:
    """Binds a fresh correlation ID (``logging_setup.correlation_scope``) for the
    duration of each HTTP request, so every log line a request's handling produces —
    across every module, via the shared ``n8n_operator`` logger namespace — carries
    the same ID, and the *next* request never inherits it. Applied unconditionally
    (unlike :class:`_TransportSecurityMiddleware`, which only guards a non-loopback
    bind): correlating log lines is useful regardless of bind, and costs nothing a
    loopback deployment needs to opt out of.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        with correlation_scope():
            await self._app(scope, receive, send)


def serve_stdio(settings: Settings, session_factory: sessionmaker[Session]) -> None:
    """Run the MCP server over stdio (the default transport). Blocks until the client
    disconnects. Always local — the parent process launched this one (boundary B9)."""
    server = build_server(settings, session_factory, caller_is_local=True, is_stdio=True)
    server.run(transport="stdio")


def serve_http(settings: Settings, session_factory: sessionmaker[Session]) -> None:
    """Run the MCP server over Streamable HTTP, bound to ``settings.http_bind``.

    A loopback bind needs nothing further — ``config.Settings`` already required an
    Origin allowlist (and, unless v2 OIDC identity is configured, a bearer token) for
    any *other* bind, and this function wraps the SDK's own Starlette app with
    :class:`_TransportSecurityMiddleware` to enforce them on every request whenever the
    bind is non-loopback. Bearer-token enforcement itself is skipped here specifically
    when OIDC is active — ``build_server`` has already wired the SDK's own
    ``token_verifier``-driven auth middleware into the app it returned, which enforces
    a real per-request JWT instead (ADR-014 section 1).
    """
    host, port = settings.http_bind_host_port()
    caller_is_local = _is_loopback_bind(settings.http_bind)
    server = build_server(settings, session_factory, caller_is_local=caller_is_local)

    max_request_body_size = max(DEFAULT_MAX_REQUEST_BODY_SIZE, settings.max_argument_bytes * 4)
    app: ASGIApp = server.streamable_http_app(
        host=host,
        max_request_body_size=max_request_body_size,
        event_store=None,
    )
    app = _CorrelationIdMiddleware(app)
    if not caller_is_local:
        oidc_active = settings.enable_v2 and settings.identity_mode == "oidc"
        bearer_token = None
        if not oidc_active:
            # `_validate_http_bind_guard` (config.py) guarantees this is set whenever
            # OIDC is not configured.
            configured_token = settings.http_bearer_token
            assert configured_token is not None
            bearer_token = configured_token.get_secret_value()
        app = _TransportSecurityMiddleware(
            app,
            bearer_token=bearer_token,
            allowed_origins=settings.allowed_origins(),
        )

    config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    uvicorn_server = uvicorn.Server(config)
    anyio.run(uvicorn_server.serve)
