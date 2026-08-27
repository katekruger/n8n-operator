"""``mcp/server.py``'s composition-root adapters against a mock n8n transport
(BUILD_PLAN section 12, phase 5).

``_PreflightAdapter``/``_HealthAdapter`` are the one place this codebase converts
``n8n/``'s locally-defined, duck-typed result dataclasses into the real
``core.models.PreflightResult``/``HealthCheckResult`` a ``core.service.PreflightPort``/
``HealthPort`` caller expects — exercised here against a real ``N8nPreflight``/
``N8nHealth`` wrapping a mock n8n instance, not a hand-rolled fake, so a mismatch
between the two field shapes would actually fail instead of being fake-shaped away.
"""

from __future__ import annotations

from typing import Any

from integration.mock_n8n import MockN8n
from n8n_operator.core.models import HealthCheckResult, PreflightResult
from n8n_operator.mcp.server import _HealthAdapter, _PreflightAdapter
from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.health import N8nHealth
from n8n_operator.n8n.preflight import N8nPreflight
from n8n_operator.registry.schema import Limits, Output, Trigger, WorkflowEntry

API_KEY = "sk-test-key"
N8N_WORKFLOW_ID = "n8n-workflow-1"

LIVE_DEFINITION: dict[str, Any] = {
    "id": N8N_WORKFLOW_ID,
    "name": "Live",
    "active": True,
    "nodes": [
        {
            "id": "webhook-1",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 0],
            "parameters": {"httpMethod": "POST", "path": "spike"},
        }
    ],
    "connections": {},
    "settings": {},
}


def _entry() -> WorkflowEntry:
    return WorkflowEntry.model_construct(
        id="wf.spike",
        n8n_workflow_id=N8N_WORKFLOW_ID,
        title="Spike",
        description="d",
        owner="carolyn",
        version=1,
        definition_hash=compute_definition_hash(LIVE_DEFINITION),
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=Trigger.model_construct(
            type="webhook", method="POST", path="/webhook/spike", auth="none", correlation="none"
        ),
        input_schema={"type": "object", "additionalProperties": False},
        output=Output(),
        limits=Limits(),
        tags=[],
        enabled=True,
    )


def _client(mock_n8n: MockN8n) -> N8nClient:
    return N8nClient(
        base_url="https://n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )


def test_preflight_adapter_produces_a_real_core_preflight_result() -> None:
    mock_n8n = MockN8n()
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, LIVE_DEFINITION)
    adapter = _PreflightAdapter(N8nPreflight(_client(mock_n8n)))

    result = adapter.check(_entry())

    assert isinstance(result, PreflightResult)
    assert result.ready is True
    assert all(
        c.status in ("pass", "warn", "unverifiable", "skipped", "fail") for c in result.checks
    )
    assert {c.check for c in result.checks} >= {"instance_reachable", "workflow_exists"}


def test_preflight_adapter_reports_not_ready_when_unreachable() -> None:
    mock_n8n = MockN8n()
    mock_n8n.unreachable = True
    adapter = _PreflightAdapter(N8nPreflight(_client(mock_n8n)))

    result = adapter.check(_entry())

    assert result.ready is False
    reachable_check = next(c for c in result.checks if c.check == "instance_reachable")
    assert reachable_check.status == "fail"
    assert reachable_check.code == "INSTANCE_UNREACHABLE"


def test_health_adapter_produces_a_real_core_health_check_result() -> None:
    mock_n8n = MockN8n()
    mock_n8n.healthy = True
    mock_n8n.api_version = "1.1.1"
    adapter = _HealthAdapter(N8nHealth(_client(mock_n8n)))

    result = adapter.check()

    assert isinstance(result, HealthCheckResult)
    assert result.reachable is True
    assert result.n8n_version == "1.1.1"
