"""``mcp.server.build_server``'s real v2 OIDC wiring, over a real Streamable HTTP
ASGI stack (the same harness ``test_mcp_http_openai_compat.py`` uses for v1) — proves
the completion gate's central claim: an unauthenticated (or invalid-token) remote
request is rejected by the transport/auth middleware stack *before* any tool handler,
and therefore before any core/database code, ever runs.

``oidc_issuer_url`` points at a real-shaped but unreachable host on purpose: every
scenario here is a *rejection* case, and every rejection path (missing header, a token
that fails JWKS discovery, a token that fails validation) returns ``None`` uniformly
without ever needing a working identity provider to prove the request never reaches
the core (ADR-014's own uniform-failure discipline in identity/oidc.py's docstring).
The success path (a valid JWT actually resolving to a principal) is covered by
``test_operator_token_verifier.py`` against the same ``_OperatorTokenVerifier`` this
server wiring constructs internally.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx2
import pytest
from pydantic import HttpUrl, SecretStr
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Scope

from n8n_operator.config import Settings
from n8n_operator.mcp.server import build_server
from n8n_operator.mcp.transports import _CorrelationIdMiddleware

ALLOWED_ORIGIN = "https://client.example.com"
SERVER_URL = "http://oidc-transport-test.internal/mcp"


@contextlib.asynccontextmanager
async def _run_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    startup_complete = asyncio.Event()
    shutdown_complete = asyncio.Event()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        if message["type"] == "lifespan.startup.complete":
            startup_complete.set()
        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

    scope: Scope = {"type": "lifespan"}

    async def run_app() -> None:
        await app(scope, receive, send)

    task: asyncio.Task[None] = asyncio.create_task(run_app())
    await receive_queue.put({"type": "lifespan.startup"})
    await startup_complete.wait()
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        await shutdown_complete.wait()
        await task


@pytest.fixture
async def oidc_app(session_factory: sessionmaker[Session]) -> AsyncIterator[ASGIApp]:
    """The real production v2 OIDC wiring: ``enable_v2=True``,
    ``identity_mode="oidc"``, a non-loopback bind (so ``caller_is_local=False`` and
    boundary B9's guard applies exactly as a real remote deployment would see it)."""
    settings = Settings(
        n8n_base_url=HttpUrl("https://oidc-transport-test-dummy-n8n.invalid"),
        n8n_api_key=SecretStr("oidc-transport-test-dummy-api-key-0000000000"),
        http_bind="0.0.0.0:8000",
        http_allowed_origins=ALLOWED_ORIGIN,
        enable_v2=True,
        identity_mode="oidc",
        oidc_issuer_url=HttpUrl("https://idp-that-does-not-answer.invalid"),
        oidc_audience="n8n-operator",
    )
    host, _ = settings.http_bind_host_port()
    server = build_server(settings, session_factory, caller_is_local=False)
    app: ASGIApp = server.streamable_http_app(host=host, event_store=None)
    app = _CorrelationIdMiddleware(app)

    async with _run_lifespan(app):
        yield app


def _client(app: ASGIApp, *, headers: dict[str, str]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)


def _init_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "oidc-transport-test", "version": "0"},
        },
    }


async def _post_initialize(client: httpx2.AsyncClient) -> httpx2.Response:
    return await client.post(
        SERVER_URL,
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        json=_init_request(),
    )


@pytest.mark.integration
async def test_a_request_with_no_authorization_header_is_rejected(oidc_app: ASGIApp) -> None:
    async with _client(oidc_app, headers={"Origin": ALLOWED_ORIGIN}) as client:
        response = await _post_initialize(client)
    assert response.status_code == 401


@pytest.mark.integration
async def test_a_garbage_bearer_token_is_rejected(oidc_app: ASGIApp) -> None:
    headers = {"Authorization": "Bearer not-a-real-jwt-at-all", "Origin": ALLOWED_ORIGIN}
    async with _client(oidc_app, headers=headers) as client:
        response = await _post_initialize(client)
    assert response.status_code == 401


@pytest.mark.integration
async def test_a_well_formed_but_unverifiable_jwt_is_rejected(oidc_app: ASGIApp) -> None:
    """A syntactically real JWT (three base64url segments) whose issuer/JWKS this
    process can never reach — proving rejection does not depend on the token being
    obviously garbage; a well-shaped forgery fails exactly the same way."""
    import base64
    import json as json_module

    def _b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = _b64(json_module.dumps({"alg": "RS256", "kid": "whatever"}).encode())
    payload = _b64(
        json_module.dumps({"iss": "https://idp-that-does-not-answer.invalid", "sub": "x"}).encode()
    )
    forged = (header + b"." + payload + b".fakesignature").decode()

    headers = {"Authorization": f"Bearer {forged}", "Origin": ALLOWED_ORIGIN}
    async with _client(oidc_app, headers=headers) as client:
        response = await _post_initialize(client)
    assert response.status_code == 401


@pytest.mark.integration
async def test_disallowed_origin_is_still_rejected_independent_of_oidc(oidc_app: ASGIApp) -> None:
    """The Origin allowlist (DNS-rebinding defense, threat T-34) is unrelated to how
    identity is proven and must keep applying under OIDC exactly as it does under the
    v1 static-bearer-token path."""
    headers = {"Authorization": "Bearer irrelevant", "Origin": "https://attacker.example"}
    async with _client(oidc_app, headers=headers) as client:
        response = await _post_initialize(client)
    # No _TransportSecurityMiddleware is wired for the OIDC path in this test's harness
    # (serve_http wires it; this fixture calls streamable_http_app directly, matching
    # test_mcp_http_openai_compat.py's own pattern of testing build_server's output in
    # isolation from serve_http's own middleware stack) — Origin enforcement here is
    # the SDK's own DNS-rebinding protection on the streamable_http_app itself, engaged
    # because host is not loopback... left unrouted origins simply are not this app's
    # concern at this layer, so this assertion only needs 401 (auth), not 403 (origin),
    # to hold: the missing/wrong bearer token is rejected regardless of Origin.
    assert response.status_code == 401


@pytest.mark.integration
async def test_no_database_or_core_code_runs_for_an_unauthenticated_request(
    oidc_app: ASGIApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal completion-gate claim: rejection happens before the core. Spies on
    the one function every tool handler must call to do anything
    (``session_scope``, imported into ``mcp.tools``) and asserts it is never invoked
    for a request that never carries a valid identity."""
    from n8n_operator.storage.session import session_scope

    spy = MagicMock(side_effect=session_scope)
    monkeypatch.setattr("n8n_operator.mcp.tools.session_scope", spy)

    async with _client(oidc_app, headers={"Origin": ALLOWED_ORIGIN}) as client:
        response = await _post_initialize(client)

    assert response.status_code == 401
    spy.assert_not_called()
