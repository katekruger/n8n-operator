"""stdio and Streamable HTTP transports.

stdio is the default: the parent process is the security boundary and no network
listener exists. Streamable HTTP binds ``127.0.0.1`` by default; a non-loopback bind
requires a bearer token **and** an Origin allowlist, or startup fails (boundary B9,
AC-20). The Origin check is DNS-rebinding defense (threat T-34).

``config.Settings`` already refuses to *construct* on a non-loopback ``http_bind``
without both a bearer token and a non-empty Origin allowlist (its own
``_validate_http_bind_guard``) — that is the authoritative "refuses to start"
enforcement B9 asks for. :class:`_TransportSecurityMiddleware` here is the second half:
*actual per-request* rejection of a missing/invalid bearer token or a missing/
disallowed ``Origin`` header, which the installed MCP SDK's own
``TransportSecuritySettings`` does not fully provide (it treats an *absent* Origin
header as same-origin and lets it through — the wrong default for a listener that must
reject exactly that). It is applied only on a non-loopback bind; a loopback bind is
unreachable from anywhere but a local process to begin with.

Phase 5 (BUILD_PLAN section 12).
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
from n8n_operator.mcp.server import build_server

__all__ = ["serve_http", "serve_stdio"]


def _is_loopback_bind(bind: str) -> bool:
    host = bind.rsplit(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


class _TransportSecurityMiddleware:
    """Rejects a non-loopback Streamable HTTP request with a missing/disallowed
    ``Origin`` or a missing/invalid bearer ``Authorization`` header, before it reaches
    the MCP session machinery (boundary B9, AC-20).

    Constructed only when the bind is non-loopback, at which point ``config.Settings``
    already guarantees ``bearer_token`` is set and ``allowed_origins`` is non-empty —
    this class does not re-derive that guarantee, it enforces it per request.
    """

    def __init__(
        self, app: ASGIApp, *, bearer_token: str, allowed_origins: tuple[str, ...]
    ) -> None:
        self._app = app
        self._bearer_token = bearer_token
        self._expected_authorization = f"Bearer {bearer_token}".encode()
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

        authorization = headers.get("authorization", "").encode()
        if not hmac.compare_digest(authorization, self._expected_authorization):
            await JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)(
                scope, receive, send
            )
            return

        await self._app(scope, receive, send)


def serve_stdio(settings: Settings, session_factory: sessionmaker[Session]) -> None:
    """Run the MCP server over stdio (the default transport). Blocks until the client
    disconnects. Always local — the parent process launched this one (boundary B9)."""
    server = build_server(settings, session_factory, caller_is_local=True)
    server.run(transport="stdio")


def serve_http(settings: Settings, session_factory: sessionmaker[Session]) -> None:
    """Run the MCP server over Streamable HTTP, bound to ``settings.http_bind``.

    A loopback bind needs nothing further — ``config.Settings`` already required a
    bearer token and an Origin allowlist for any *other* bind, and this function wraps
    the SDK's own Starlette app with :class:`_TransportSecurityMiddleware` to enforce
    them on every request whenever the bind is non-loopback.
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
    if not caller_is_local:
        # `_validate_http_bind_guard` (config.py) already guarantees both are set.
        bearer_token = settings.http_bearer_token
        assert bearer_token is not None
        app = _TransportSecurityMiddleware(
            app,
            bearer_token=bearer_token.get_secret_value(),
            allowed_origins=settings.allowed_origins(),
        )

    config = uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    uvicorn_server = uvicorn.Server(config)
    anyio.run(uvicorn_server.serve)
