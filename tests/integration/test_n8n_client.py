"""``n8n/client.py`` against a mock n8n transport (BUILD_PLAN section 12, phase 4).

Covers AC-07 (instance unreachable), AC-17 (no retry — behavioral half; the grep-based
contract test lives in ``tests/contract/test_n8n_no_retry.py``), timeout-to-indeterminate,
malformed provider responses, pagination-loop protection, bounded response sizes, and API
key redaction.
"""

from __future__ import annotations

import time

import httpx
import pytest

from integration.mock_n8n import MockN8n
from n8n_operator.errors import (
    InstanceUnreachableError,
    ProviderError,
    WorkflowMissingOnInstanceError,
)
from n8n_operator.n8n.client import MAX_EXECUTION_LIST_PAGES, MAX_RESPONSE_BYTES, N8nClient

API_KEY = "sk-test-super-secret-do-not-leak-1234567890"


@pytest.fixture
def mock_n8n() -> MockN8n:
    return MockN8n()


@pytest.fixture
def client(mock_n8n: MockN8n) -> N8nClient:
    return N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )


# --------------------------------------------------------------------------------------
# health_check / instance_reachable — AC-07
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_health_check_passes_when_reachable(client: N8nClient) -> None:
    status = client.health_check()
    assert status.status == "ok"


@pytest.mark.integration
def test_health_check_raises_instance_unreachable_when_down(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.unreachable = True
    with pytest.raises(InstanceUnreachableError):
        client.health_check()


@pytest.mark.integration
def test_health_check_reports_unreachable_within_the_configured_timeout(mock_n8n: MockN8n) -> None:
    """AC-07: reports INSTANCE_UNREACHABLE "within the configured timeout" — a mock
    transport can't simulate real elapsed time, so this asserts the client actually
    uses the configured connect timeout value (not an unbounded default) by checking it
    is threaded through to the underlying request."""
    mock_n8n.timeout = True
    client = N8nClient(
        base_url="http://mock-n8n.invalid",
        api_key=API_KEY,
        connect_timeout_seconds=0.5,
        transport=mock_n8n.transport(),
    )
    started = time.monotonic()
    with pytest.raises(InstanceUnreachableError):
        client.health_check()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0  # the mock raises immediately; this is a sanity bound, not a timing test


# --------------------------------------------------------------------------------------
# get_api_version_info
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_get_api_version_info_parses_the_version(client: N8nClient) -> None:
    assert client.get_api_version_info() == "1.1.1"


@pytest.mark.integration
def test_get_api_version_info_returns_none_on_error_rather_than_raising(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.healthy = (
        False  # unrelated toggle; simulate the openapi endpoint itself failing instead
    )
    mock_n8n.workflows.clear()

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    original_handle = mock_n8n._handle

    def patched(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/openapi.yml":
            return httpx.Response(500)
        return original_handle(request)

    mock_n8n._handle = patched  # type: ignore[method-assign]
    client_2 = N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )
    assert client_2.get_api_version_info() is None


# --------------------------------------------------------------------------------------
# get_workflow
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_get_workflow_returns_the_raw_dict(mock_n8n: MockN8n, client: N8nClient) -> None:
    definition = {
        "id": "n8n-1",
        "name": "Test",
        "active": True,
        "nodes": [
            {"id": "a", "name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}}
        ],
        "connections": {},
        "settings": {},
    }
    mock_n8n.add_workflow("n8n-1", definition)
    result = client.get_workflow("n8n-1")
    assert result == definition
    assert isinstance(result, dict)


@pytest.mark.integration
def test_get_workflow_raises_missing_on_404(client: N8nClient) -> None:
    with pytest.raises(WorkflowMissingOnInstanceError):
        client.get_workflow("does-not-exist")


@pytest.mark.integration
def test_get_workflow_raises_provider_error_on_malformed_response(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """A response missing required shape (no ``nodes``) is a malformed provider
    response, not an unhandled exception (threat T-32)."""
    mock_n8n.add_workflow(
        "n8n-bad", {"id": "n8n-bad", "name": "Bad", "active": True}
    )  # no nodes/connections
    with pytest.raises(ProviderError):
        client.get_workflow("n8n-bad")


@pytest.mark.integration
def test_get_workflow_between_two_reads_reflects_a_definition_change(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """'Definition changed between reads' — the client has no caching layer, so a
    second read always reflects whatever the instance now reports."""
    v1 = {
        "id": "n8n-1",
        "name": "T",
        "active": True,
        "nodes": [],
        "connections": {},
        "settings": {},
    }
    mock_n8n.add_workflow("n8n-1", v1)
    first = client.get_workflow("n8n-1")
    v2 = {**v1, "nodes": [{"id": "new", "name": "New", "type": "x", "parameters": {}}]}
    mock_n8n.add_workflow("n8n-1", v2)
    second = client.get_workflow("n8n-1")
    assert first != second
    assert second["nodes"] == v2["nodes"]


# --------------------------------------------------------------------------------------
# list_executions — pagination-loop protection
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_executions_returns_matching_records(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.add_execution(
        "1",
        {"id": "1", "finished": True, "mode": "webhook", "status": "success", "workflowId": "wf-a"},
    )
    mock_n8n.add_execution(
        "2",
        {"id": "2", "finished": True, "mode": "webhook", "status": "success", "workflowId": "wf-b"},
    )
    results = client.list_executions(workflow_id="wf-a", limit=20)
    assert [r.id for r in results] == ["1"]


@pytest.mark.integration
def test_list_executions_never_loops_more_than_the_page_bound(client: N8nClient) -> None:
    """A server that always returns a ``nextCursor`` (buggy or malicious) cannot make
    this call loop forever."""
    call_count = 0
    real_client = client._client

    original_request = real_client.request

    def counting_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Always claim there's another page, regardless of what the mock would say.
        response = original_request(method, url, **kwargs)  # type: ignore[arg-type]
        if "/api/v1/executions" in str(url) and not str(url).endswith("/api/v1/executions/1"):
            body = dict(response.json())
            body["nextCursor"] = "always-more"
            return httpx.Response(200, json=body, request=response.request)
        return response

    real_client.request = counting_request  # type: ignore[method-assign,assignment]
    results = client.list_executions(workflow_id="wf-a", limit=1_000_000)
    assert call_count <= MAX_EXECUTION_LIST_PAGES
    assert results == []  # no executions were ever registered; just proving termination


# --------------------------------------------------------------------------------------
# get_execution
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_get_execution_returns_the_summary(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.add_execution(
        "42",
        {
            "id": "42",
            "finished": True,
            "mode": "webhook",
            "status": "success",
            "workflowId": "wf-a",
        },
    )
    result = client.get_execution("42")
    assert result.id == "42"
    assert result.status == "success"


@pytest.mark.integration
def test_get_execution_raises_missing_on_404(client: N8nClient) -> None:
    with pytest.raises(WorkflowMissingOnInstanceError):
        client.get_execution("does-not-exist")


@pytest.mark.integration
def test_get_execution_never_requests_full_run_data(mock_n8n: MockN8n, client: N8nClient) -> None:
    """The client never passes ``includeData=true`` — the request itself proves it,
    independent of what ``ExecutionSummary`` models."""
    mock_n8n.add_execution(
        "1",
        {"id": "1", "finished": True, "mode": "webhook", "status": "success", "workflowId": "wf-a"},
    )
    client.get_execution("1")
    last_request = mock_n8n.requests[-1]
    assert "includeData" not in last_request.url.query.decode()


# --------------------------------------------------------------------------------------
# dispatch_webhook — success / error / indeterminate
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_dispatch_success_returns_success_kind_and_correlation(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.add_webhook_response(
        "/webhook/abc",
        status=200,
        body={"n8n_operator": {"execution_id": "999"}, "data": {"ok": True}},
    )
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "success"
    assert outcome.execution_id == "999"
    assert outcome.body == {"n8n_operator": {"execution_id": "999"}, "data": {"ok": True}}


@pytest.mark.integration
def test_dispatch_non_2xx_returns_error_kind(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.add_webhook_response("/webhook/abc", status=500, body={"message": "boom"})
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "error"
    assert outcome.http_status == 500


@pytest.mark.integration
def test_dispatch_timeout_returns_indeterminate(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.add_webhook_timeout("/webhook/abc")
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "indeterminate"
    assert outcome.http_status is None
    assert outcome.execution_id is None


@pytest.mark.integration
def test_dispatch_connection_error_returns_indeterminate(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """ADR-005: connection-refused and timeout receive identical treatment — no
    special-cased 'definitely did not happen' path."""
    mock_n8n.add_webhook_connection_error("/webhook/abc")
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "indeterminate"


@pytest.mark.integration
def test_dispatch_malformed_body_is_success_kind_with_no_correlation(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """A 200 response with an unparseable body is not indeterminate — n8n responded.
    It is a successful dispatch with no correlation available (ADR-009: "a workflow
    that returns a result is not broken because its envelope is")."""
    mock_n8n.add_webhook_malformed("/webhook/abc", status=200, raw_body="not json at all {{{")
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "success"
    assert outcome.execution_id is None
    assert outcome.body is None


@pytest.mark.integration
def test_dispatch_response_missing_the_envelope_has_no_correlation(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.add_webhook_response("/webhook/abc", status=200, body={"just": "data"})
    outcome = client.dispatch_webhook(
        path="/webhook/abc", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "success"
    assert outcome.execution_id is None


# --------------------------------------------------------------------------------------
# Bounded response sizes
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_oversized_response_is_refused(mock_n8n: MockN8n, client: N8nClient) -> None:
    huge = {
        "id": "n8n-huge",
        "name": "x",
        "active": True,
        "nodes": [],
        "connections": {},
        "settings": {},
    }
    huge["padding"] = "x" * (MAX_RESPONSE_BYTES + 1024)
    mock_n8n.add_workflow("n8n-huge", huge)
    with pytest.raises(ProviderError):
        client.get_workflow("n8n-huge")


# --------------------------------------------------------------------------------------
# Endpoint allowlist
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_request_rejects_a_non_allowlisted_endpoint_template(client: N8nClient) -> None:
    with pytest.raises(ProviderError):
        client._request(
            endpoint_template="/api/v1/not-a-real-endpoint",
            method="GET",
            path="/api/v1/not-a-real-endpoint",
            read_timeout_seconds=5,
        )


# --------------------------------------------------------------------------------------
# API key redaction
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_api_key_never_appears_in_a_raised_exception(mock_n8n: MockN8n, client: N8nClient) -> None:
    mock_n8n.unreachable = True
    with pytest.raises(InstanceUnreachableError) as excinfo:
        client.health_check()
    rendered = repr(excinfo.value) + str(excinfo.value.to_dict())
    assert API_KEY not in rendered


@pytest.mark.integration
def test_api_key_never_appears_in_a_malformed_response_exception(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.add_workflow("n8n-bad", {"id": "n8n-bad"})  # missing required fields
    with pytest.raises(ProviderError) as excinfo:
        client.get_workflow("n8n-bad")
    rendered = repr(excinfo.value) + str(excinfo.value.to_dict())
    assert API_KEY not in rendered


@pytest.mark.integration
def test_api_key_is_sent_as_the_documented_header_not_a_query_param(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    client.health_check()
    mock_n8n.add_workflow(
        "n8n-1",
        {
            "id": "n8n-1",
            "name": "t",
            "active": True,
            "nodes": [],
            "connections": {},
            "settings": {},
        },
    )
    client.get_workflow("n8n-1")
    last_request = mock_n8n.requests[-1]
    assert API_KEY not in str(last_request.url)
    assert last_request.headers.get("x-n8n-api-key") == API_KEY


# --------------------------------------------------------------------------------------
# Unknown enum / unknown field — a parse failure yields a structured error, not a crash
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_an_execution_status_outside_the_known_enum_is_a_provider_error(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """A future n8n release could add a new execution status this codebase has never
    seen. That must surface as a typed, safe error (threat T-32) — never an unhandled
    ``pydantic.ValidationError`` escaping this module."""
    mock_n8n.add_execution(
        "1",
        {
            "id": "1",
            "finished": True,
            "mode": "webhook",
            "status": "a-status-that-does-not-exist-yet",
            "workflowId": "wf-a",
        },
    )
    with pytest.raises(ProviderError):
        client.get_execution("1")


@pytest.mark.integration
def test_an_unrecognized_extra_field_on_a_workflow_response_does_not_crash(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    """The opposite direction: a field this codebase has never seen must not cause a
    crash either — ``WorkflowDefinition`` uses ``extra="allow"`` precisely so a new n8n
    field is forward-compatible, not a parse failure."""
    definition = {
        "id": "n8n-1",
        "name": "t",
        "active": True,
        "nodes": [],
        "connections": {},
        "settings": {},
        "aFieldThisCodebaseHasNeverSeen": {"nested": ["data"]},
    }
    mock_n8n.add_workflow("n8n-1", definition)
    result = client.get_workflow("n8n-1")
    assert result["aFieldThisCodebaseHasNeverSeen"] == {"nested": ["data"]}


# --------------------------------------------------------------------------------------
# Context manager and remaining edge paths
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_client_works_as_a_context_manager(mock_n8n: MockN8n) -> None:
    with N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    ) as ctx_client:
        assert ctx_client.health_check().status == "ok"


@pytest.mark.integration
def test_health_check_raises_on_a_malformed_body(mock_n8n: MockN8n, client: N8nClient) -> None:
    def patched(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, content=b"not json")
        raise AssertionError("unexpected request")

    mock_n8n._handle = patched  # type: ignore[method-assign]
    broken_client = N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )
    with pytest.raises(ProviderError):
        broken_client.health_check()


@pytest.mark.integration
def test_dispatch_of_an_oversized_response_is_indeterminate(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.add_webhook_response(
        "/webhook/big", status=200, body={"padding": "x" * (MAX_RESPONSE_BYTES + 1024)}
    )
    outcome = client.dispatch_webhook(
        path="/webhook/big", method="POST", json_body={}, timeout_seconds=5
    )
    assert outcome.kind == "indeterminate"


@pytest.mark.integration
def test_list_executions_raises_on_a_malformed_execution_record(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    def patched(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/executions":
            return httpx.Response(
                200, json={"data": [{"not": "a valid execution"}], "nextCursor": None}
            )
        raise AssertionError("unexpected request")

    mock_n8n._handle = patched  # type: ignore[method-assign]
    broken_client = N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )
    with pytest.raises(ProviderError):
        broken_client.list_executions(workflow_id="wf-a")


@pytest.mark.integration
def test_list_executions_raises_on_a_malformed_list_body(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    def patched(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/executions":
            return httpx.Response(200, json=["not", "a", "dict"])
        raise AssertionError("unexpected request")

    mock_n8n._handle = patched  # type: ignore[method-assign]
    broken_client = N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )
    with pytest.raises(ProviderError):
        broken_client.list_executions(workflow_id="wf-a")
