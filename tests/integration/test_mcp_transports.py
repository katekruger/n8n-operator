"""Streamable HTTP transport security (BUILD_PLAN section 12, phase 5).

``config.Settings`` already refuses to *construct* on a non-loopback ``http_bind``
without a bearer token and an Origin allowlist (``tests/unit/test_config.py`` covers
that half of B9/AC-20 thoroughly). This module covers the other half:
:class:`n8n_operator.mcp.transports._TransportSecurityMiddleware`, which enforces both
on every actual request once the transport is up — a missing or disallowed ``Origin``
and a missing or invalid bearer token are each rejected before the request reaches the
MCP session machinery.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from n8n_operator.mcp.transports import _is_loopback_bind, _TransportSecurityMiddleware


async def _inner_endpoint(request: object) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _protected_client() -> TestClient:
    inner = Starlette(routes=[Route("/mcp", _inner_endpoint)])
    protected = _TransportSecurityMiddleware(
        inner,
        bearer_token="s3cr3t-token",
        allowed_origins=("https://client.example",),
    )
    return TestClient(protected)


def test_missing_origin_is_rejected() -> None:
    client = _protected_client()
    response = client.get("/mcp", headers={"authorization": "Bearer s3cr3t-token"})
    assert response.status_code == 403


def test_disallowed_origin_is_rejected() -> None:
    client = _protected_client()
    response = client.get(
        "/mcp",
        headers={
            "authorization": "Bearer s3cr3t-token",
            "origin": "https://attacker.example",
        },
    )
    assert response.status_code == 403


def test_missing_bearer_token_is_rejected() -> None:
    client = _protected_client()
    response = client.get("/mcp", headers={"origin": "https://client.example"})
    assert response.status_code == 401


def test_wrong_bearer_token_is_rejected() -> None:
    client = _protected_client()
    response = client.get(
        "/mcp",
        headers={"authorization": "Bearer wrong-token", "origin": "https://client.example"},
    )
    assert response.status_code == 401


def test_valid_origin_and_bearer_token_reach_the_app() -> None:
    client = _protected_client()
    response = client.get(
        "/mcp",
        headers={"authorization": "Bearer s3cr3t-token", "origin": "https://client.example"},
    )
    assert response.status_code == 200
    assert response.text == "ok"


def test_a_second_allowed_origin_also_reaches_the_app() -> None:
    inner = Starlette(routes=[Route("/mcp", _inner_endpoint)])
    protected = _TransportSecurityMiddleware(
        inner,
        bearer_token="s3cr3t-token",
        allowed_origins=("https://one.example", "https://two.example"),
    )
    client = TestClient(protected)
    response = client.get(
        "/mcp",
        headers={"authorization": "Bearer s3cr3t-token", "origin": "https://two.example"},
    )
    assert response.status_code == 200


def test_is_loopback_bind() -> None:
    assert _is_loopback_bind("127.0.0.1:8000")
    assert _is_loopback_bind("localhost:8000")
    assert _is_loopback_bind("::1:8000")
    assert not _is_loopback_bind("0.0.0.0:8000")
    assert not _is_loopback_bind("10.0.0.5:8000")
