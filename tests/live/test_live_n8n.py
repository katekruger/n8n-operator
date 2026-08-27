"""Repeatable compatibility checks against a real n8n instance.

These tests are intentionally excluded from normal CI. They only target the synthetic,
side-effect-free workflow in ``examples/registry/synthetic_test_workflow.json`` and skip
unless every required ``N8N_LIVE_*`` variable is present.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for live-n8n tests")
    return value


@pytest.fixture(scope="module")
def live_client() -> Iterator[N8nClient]:
    client = N8nClient(
        base_url=_required("N8N_LIVE_BASE_URL"),
        api_key=_required("N8N_LIVE_API_KEY"),
    )
    yield client
    client.close()


@pytest.mark.live_n8n
def test_live_instance_health_and_workflow_read(live_client: N8nClient) -> None:
    assert live_client.health_check().status == "ok"

    workflow = live_client.get_workflow(_required("N8N_LIVE_WORKFLOW_ID"))
    assert workflow["active"] is True
    assert compute_definition_hash(workflow) == compute_definition_hash(workflow)


@pytest.mark.live_n8n
def test_live_dispatch_correlation_and_execution_read(live_client: N8nClient) -> None:
    outcome = live_client.dispatch_webhook(
        path=_required("N8N_LIVE_WEBHOOK_PATH"),
        method="POST",
        json_body={"value": 21},
        timeout_seconds=30,
    )

    assert outcome.kind == "success"
    assert outcome.result == {"result": 42}
    assert outcome.correlation_available is True
    assert outcome.execution_id is not None

    execution = live_client.get_execution(outcome.execution_id)
    assert execution.workflow_id == _required("N8N_LIVE_WORKFLOW_ID")
    assert execution.status == "success"
