"""``n8n/health.py`` against a mock n8n transport (BUILD_PLAN section 12, phase 5).

Mirrors ``tests/integration/test_n8n_preflight.py``'s pattern: the mock n8n instance,
not a live one, exercises reachable/unreachable/timeout outcomes.
"""

from __future__ import annotations

import httpx
import pytest

from integration.mock_n8n import MockN8n
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.health import N8nHealth

API_KEY = "sk-test-key"


@pytest.fixture
def mock_n8n() -> MockN8n:
    return MockN8n()


@pytest.fixture
def client(mock_n8n: MockN8n) -> N8nClient:
    return N8nClient(
        base_url="https://n8n.invalid",
        api_key=API_KEY,
        transport=mock_n8n.transport(),
    )


def test_reachable_reports_version_and_latency(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.healthy = True
    mock_n8n.api_version = "1.1.1"
    result = N8nHealth(client).check()
    assert result.reachable is True
    assert result.n8n_version == "1.1.1"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0
    assert result.reason is None


def test_unreachable_reports_a_code_never_a_raw_connection_error(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.unreachable = True
    result = N8nHealth(client).check()
    assert result.reachable is False
    assert result.reason == "INSTANCE_UNREACHABLE"
    assert result.n8n_version is None
    assert result.latency_ms is None


def test_timeout_is_reported_as_unreachable_not_a_crash(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.timeout = True
    result = N8nHealth(client).check()
    assert result.reachable is False
    assert result.reason == "INSTANCE_UNREACHABLE"


def test_healthy_but_unhealthy_status_body_is_still_unreachable(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.healthy = False
    result = N8nHealth(client).check()
    assert result.reachable is False
    assert result.reason == "INSTANCE_UNREACHABLE"


def test_never_leaks_the_api_key_or_base_url_in_the_result(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.healthy = True
    result = N8nHealth(client).check()
    serialized = repr(result)
    assert API_KEY not in serialized
    assert "n8n.invalid" not in serialized


def test_malformed_openapi_spec_leaves_version_none_but_still_reachable(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """``get_api_version_info`` is documented as best-effort and never raising
    (n8n/client.py); a health check must still report ``reachable: True`` off a
    successful ``/healthz`` alone even if the version probe fails."""

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/openapi.yml":
            return httpx.Response(200, text="not: [valid, yaml: at all")
        return httpx.Response(404)

    broken_client = N8nClient(
        base_url="https://n8n.invalid",
        api_key=API_KEY,
        transport=httpx.MockTransport(_handle),
    )
    result = N8nHealth(broken_client).check()
    assert result.reachable is True
    assert result.n8n_version is None
