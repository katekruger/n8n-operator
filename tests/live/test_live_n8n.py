"""Repeatable compatibility checks against a real n8n instance.

These tests are intentionally excluded from normal CI. They only target the synthetic,
side-effect-free workflow in ``examples/registry/synthetic_test_workflow.json`` and skip
unless every required ``N8N_LIVE_*`` variable is present. None of them ever creates,
edits, activates, or deletes a workflow on the live instance — bring the synthetic
workflow up and activate it once via ``scripts/live_n8n_up.sh``, out of band.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from n8n_operator.errors import (
    InstanceUnreachableError,
    ProviderError,
    WorkflowMissingOnInstanceError,
)
from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.preflight import N8nPreflight
from n8n_operator.registry.schema import Trigger, WorkflowEntry


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


def _synthetic_entry(*, definition_hash: str) -> WorkflowEntry:
    """A ``WorkflowEntry`` matching ``examples/registry/synthetic_test_workflow.json``'s
    real trigger config — enough for ``N8nPreflight.check()`` to run every check against
    the live instance, including drift, without this test suite ever needing its own
    registry file on disk."""
    return WorkflowEntry(
        id="live.synthetic_test_workflow",
        n8n_workflow_id=_required("N8N_LIVE_WORKFLOW_ID"),
        title="n8n Operator — synthetic test workflow",
        description="Live compatibility harness target.",
        owner="live-n8n-harness",
        version=1,
        definition_hash=definition_hash,
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=Trigger(
            type="webhook",
            method="POST",
            path=_required("N8N_LIVE_WEBHOOK_PATH"),
            auth="none",
            correlation="response_envelope",
        ),
        input_schema={"type": "object", "additionalProperties": True},
    )


@pytest.mark.live_n8n
def test_live_instance_health_and_workflow_read(live_client: N8nClient) -> None:
    assert live_client.health_check().status == "ok"

    workflow_id = _required("N8N_LIVE_WORKFLOW_ID")
    workflow = live_client.get_workflow(workflow_id)
    assert workflow["id"] == workflow_id  # exact workflow-ID match, not just "a" workflow
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


@pytest.mark.live_n8n
def test_live_preflight_reports_no_drift_against_the_real_current_hash(
    live_client: N8nClient,
) -> None:
    """The positive case: a registered hash that matches the live definition passes
    every preflight check, including ``definition_unchanged`` — proves the drift
    check runs against the real instance, not just that it *can* fail."""
    workflow = live_client.get_workflow(_required("N8N_LIVE_WORKFLOW_ID"))
    current_hash = compute_definition_hash(workflow)

    result = N8nPreflight(live_client).check(_synthetic_entry(definition_hash=current_hash))

    assert result.ready is True
    checks = {c.check: c.status for c in result.checks}
    assert checks["instance_reachable"] == "pass"
    assert checks["workflow_exists"] == "pass"
    assert checks["workflow_active"] == "pass"
    assert checks["definition_unchanged"] == "pass"


@pytest.mark.live_n8n
def test_live_preflight_detects_drift_against_a_stale_hash(live_client: N8nClient) -> None:
    """The negative case: a registered hash that does *not* match the live definition
    — as if the workflow had been edited in n8n since registration — is caught by
    ``definition_unchanged`` and the whole preflight refuses readiness. Never edits
    the real workflow; the "drift" is a deliberately wrong hash given to the checker,
    exactly how a real registration would go stale."""
    stale_hash = "sha256:" + "0" * 64

    result = N8nPreflight(live_client).check(_synthetic_entry(definition_hash=stale_hash))

    assert result.ready is False
    checks = {c.check: c.status for c in result.checks}
    assert checks["definition_unchanged"] == "fail"


@pytest.mark.live_n8n
def test_live_wrong_api_key_fails_cleanly() -> None:
    """An invalid credential must fail as a clear, typed error — never crash, never
    silently authenticate as something else."""
    client = N8nClient(
        base_url=_required("N8N_LIVE_BASE_URL"),
        api_key="definitely-not-a-valid-key-00000000",
    )
    try:
        with pytest.raises(ProviderError) as excinfo:
            client.get_workflow(_required("N8N_LIVE_WORKFLOW_ID"))
        assert excinfo.value.details.get("status_code") == 401
    finally:
        client.close()


@pytest.mark.live_n8n
def test_live_wrong_workflow_id_fails_cleanly(live_client: N8nClient) -> None:
    with pytest.raises(WorkflowMissingOnInstanceError):
        live_client.get_workflow("wf_definitely_does_not_exist_00000000")


@pytest.mark.live_n8n
def test_live_wrong_webhook_path_dispatches_as_an_error_not_a_crash(
    live_client: N8nClient,
) -> None:
    """n8n responds (404, the webhook isn't registered) rather than the connection
    failing — a real response that arrived, so this is ``kind="error"``, never
    ``"indeterminate"`` (ADR-009: indeterminate means no confirmed response, not "a
    response we didn't like")."""
    outcome = live_client.dispatch_webhook(
        path="/webhook/this-path-does-not-exist-00000000",
        method="POST",
        json_body={"value": 21},
        timeout_seconds=30,
    )
    assert outcome.kind == "error"
    assert outcome.http_status == 404


@pytest.mark.live_n8n
def test_live_instance_unavailable_fails_cleanly() -> None:
    """A base URL nothing is listening on must fail as
    ``InstanceUnreachableError`` — never hang, never a raw connection-refused
    traceback reaching the caller."""
    client = N8nClient(
        base_url="http://127.0.0.1:1",  # a privileged, essentially never-bound port
        api_key=_required("N8N_LIVE_API_KEY"),
    )
    try:
        with pytest.raises(InstanceUnreachableError):
            client.health_check()
    finally:
        client.close()
