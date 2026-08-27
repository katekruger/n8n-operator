"""Automated Streamable HTTP compatibility check matching the shape a real remote MCP
client — OpenAI's Responses API `mcp` tool included — sends (BUILD_PLAN section 12,
phase 5 continuation).

The Responses API's documented `mcp` tool object
(https://developers.openai.com/api/docs/api-reference/responses/create, `tools[].mcp`)
accepts `server_url`, an `authorization` OAuth-token field, and a `headers` map
("Optional HTTP headers to send to the MCP server. Use for authentication or other
purposes.") — the same `Authorization: Bearer <token>` plus explicit `Origin` shape
`examples/mcp-clients/openai_responses_tool.json` already documents. This test drives
that exact shape against the real production server-construction path
(`mcp.server.build_server`, and the same middleware stack `mcp.transports.serve_http`
wires) — not a hand-rolled substitute — so a regression in either the transport
security enforcement or the tool/resource surface would show up here exactly as it
would to a real remote client.

Settings are constructed with a non-loopback `http_bind` (`0.0.0.0:8000`) purely as
configuration — this test never opens a real socket, so no port is ever actually bound
to anything. That's enough to make `config.Settings`' own guard require a bearer token
and an Origin allowlist (boundary B9), and to pass `caller_is_local=False` into
`build_server`, exactly as a real non-loopback deployment would. The app runs
in-process over an ASGI transport, driven through the real Starlette lifespan protocol
(the streamable-HTTP session manager's task group must actually start, the same as
under uvicorn) so this is a genuine end-to-end MCP session, not a bypassed shortcut.

What this is NOT: a real hosted OpenAI Responses API call. That needs a publicly
reachable TLS endpoint and real OpenAI credentials, neither available here — see
`examples/mcp-clients/README.md`'s note on exactly what a live OpenAI-connector run
would still verify beyond this.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from pydantic import HttpUrl, SecretStr
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Scope

from n8n_operator.config import Settings
from n8n_operator.core import service
from n8n_operator.mcp.server import build_server
from n8n_operator.mcp.transports import _CorrelationIdMiddleware, _TransportSecurityMiddleware
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import session_scope

from .test_mcp_tools import REGISTRY_YAML

BEARER_TOKEN = "openai-compat-test-bearer-token-0000000000"
ALLOWED_ORIGIN = "https://platform.openai.com"
SERVER_URL = "http://openai-compat-test.internal/mcp"

EXPECTED_TOOL_NAMES = {
    "list_workflows",
    "describe_workflow",
    "get_instance_health",
    "validate_input",
    "preflight_workflow",
    "prepare_operation",
    "get_operation",
    "execute_operation",
    "cancel_operation",
    "list_operations",
    "get_execution_result",
    "get_execution_log",
}


@pytest.fixture
def loaded_session_factory(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> sessionmaker[Session]:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


@contextlib.asynccontextmanager
async def _run_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drives the ASGI lifespan protocol by hand: `Starlette`'s own `lifespan=` for
    this app is `session_manager.run()` (the streamable-HTTP session manager's task
    group) — nothing works until `lifespan.startup` actually completes, the same as
    under a real ASGI server. httpx's `ASGITransport` deliberately does not manage
    lifespan itself, so a real hosted-shape test needs this either way."""
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
async def openai_shaped_app(
    loaded_session_factory: sessionmaker[Session],
) -> AsyncIterator[ASGIApp]:
    """The real production Streamable HTTP wiring — `build_server` plus the exact
    middleware stack `serve_http` applies — configured non-loopback, requiring the
    same bearer token + Origin allowlist boundary B9 demands of any real remote
    deployment."""
    settings = Settings(
        n8n_base_url=HttpUrl("https://openai-compat-test-dummy-n8n-instance.invalid"),
        n8n_api_key=SecretStr("openai-compat-test-dummy-api-key-0000000000"),
        http_bind="0.0.0.0:8000",
        http_bearer_token=SecretStr(BEARER_TOKEN),
        http_allowed_origins=ALLOWED_ORIGIN,
    )
    host, _ = settings.http_bind_host_port()

    server = build_server(settings, loaded_session_factory, caller_is_local=False)
    app: ASGIApp = server.streamable_http_app(
        host=host, max_request_body_size=DEFAULT_MAX_REQUEST_BODY_SIZE, event_store=None
    )
    app = _CorrelationIdMiddleware(app)
    app = _TransportSecurityMiddleware(
        app, bearer_token=BEARER_TOKEN, allowed_origins=(ALLOWED_ORIGIN,)
    )

    async with _run_lifespan(app):
        yield app


def _client(app: ASGIApp, *, headers: dict[str, str]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)


def _init_request(request_id: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "openai-compat-test", "version": "0"},
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


async def test_full_session_with_the_documented_openai_header_shape(
    openai_shaped_app: ASGIApp,
) -> None:
    """`Authorization: Bearer <token>` plus an explicit `Origin` — exactly what
    populating the Responses API `mcp` tool's `headers` map per its documented shape
    would send. Covers initialize, the identical 12-tool surface stdio serves (AC-23),
    a safe tool call, and session continuation (a second call on the same session)."""
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}", "Origin": ALLOWED_ORIGIN}
    async with (
        _client(openai_shaped_app, headers=headers) as http_client,
        streamable_http_client(SERVER_URL, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        init_result = await session.initialize()
        assert init_result.server_info.name == "n8n-operator"

        tools_result = await session.list_tools()
        assert {tool.name for tool in tools_result.tools} == EXPECTED_TOOL_NAMES

        first_call = await session.call_tool("list_workflows", {})
        assert not first_call.is_error

        # Session continuation: a second call over the same session, no re-initialize.
        second_call = await session.call_tool("list_workflows", {})
        assert not second_call.is_error


async def test_missing_bearer_token_is_rejected_over_a_real_session(
    openai_shaped_app: ASGIApp,
) -> None:
    headers = {"Origin": ALLOWED_ORIGIN}
    async with _client(openai_shaped_app, headers=headers) as http_client:
        response = await _post_initialize(http_client)
        assert response.status_code == 401


async def test_missing_origin_is_rejected_over_a_real_session(
    openai_shaped_app: ASGIApp,
) -> None:
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    async with _client(openai_shaped_app, headers=headers) as http_client:
        response = await _post_initialize(http_client)
        assert response.status_code == 403


async def test_disallowed_origin_is_rejected_over_a_real_session(
    openai_shaped_app: ASGIApp,
) -> None:
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}", "Origin": "https://attacker.example"}
    async with _client(openai_shaped_app, headers=headers) as http_client:
        response = await _post_initialize(http_client)
        assert response.status_code == 403


async def test_http_tool_and_resource_surface_matches_stdio(
    openai_shaped_app: ASGIApp,
) -> None:
    """AC-23: the same twelve tools and two resources, over Streamable HTTP as over
    stdio (`scripts/mcp_session_smoke.py` asserts the stdio half)."""
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}", "Origin": ALLOWED_ORIGIN}
    async with (
        _client(openai_shaped_app, headers=headers) as http_client,
        streamable_http_client(SERVER_URL, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        resources_result = await session.list_resources()
        resource_uris = {str(r.uri) for r in resources_result.resources}
        templates_result = await session.list_resource_templates()
        resource_uris |= {t.uri_template for t in templates_result.resource_templates}
        assert {"registry://workflows", "audit://operations/{operation_id}"}.issubset(resource_uris)
