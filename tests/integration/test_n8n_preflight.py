"""``n8n/preflight.py`` against a mock n8n transport (BUILD_PLAN section 12, phase 4).

Covers AC-05 (inactive), AC-06 (drift), AC-07 (unreachable), AC-30 (correlation warn,
credential bindings vs. validity), and the trigger-compatibility / unattended-execution
checks this phase adds.
"""

from __future__ import annotations

from typing import Any

import pytest

from integration.mock_n8n import MockN8n
from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient
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
            "parameters": {"httpMethod": "POST", "path": "spike-test"},
        },
        {
            "id": "http-1",
            "name": "HTTP Call",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [200, 0],
            "parameters": {
                "url": "https://example.invalid",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "httpBasicAuth",
            },
            "credentials": {"httpBasicAuth": {"id": "cred-1", "name": "cred"}},
        },
    ],
    "connections": {},
    "settings": {},
}


def make_entry(**overrides: Any) -> WorkflowEntry:
    base: dict[str, Any] = {
        "id": "wf.spike",
        "n8n_workflow_id": N8N_WORKFLOW_ID,
        "title": "Spike",
        "description": "d",
        "owner": "carolyn",
        "version": 1,
        "definition_hash": compute_definition_hash(LIVE_DEFINITION),
        "risk": "medium",
        "side_effects": "external_write",
        "approval": "required",
        "trigger": Trigger.model_construct(
            type="webhook",
            method="POST",
            path="/webhook/spike-test",
            auth="none",
            correlation="none",
        ),
        "input_schema": {"type": "object"},
        "output": Output(),
        "limits": Limits(),
    }
    base.update(overrides)
    return WorkflowEntry.model_construct(**base)


@pytest.fixture
def mock_n8n() -> MockN8n:
    mock = MockN8n()
    mock.add_workflow(N8N_WORKFLOW_ID, LIVE_DEFINITION)
    mock.api_version = "1.1.1"
    return mock


@pytest.fixture
def client(mock_n8n: MockN8n) -> N8nClient:
    return N8nClient(
        base_url="http://mock-n8n.invalid", api_key=API_KEY, transport=mock_n8n.transport()
    )


@pytest.fixture
def preflight(client: N8nClient) -> N8nPreflight:
    return N8nPreflight(client, supported_api_versions=frozenset({"1.1.1"}))


def _check(result: Any, name: str) -> Any:
    matching = [c for c in result.checks if c.check == name]
    assert len(matching) == 1, f"expected exactly one {name!r} check, found {len(matching)}"
    return matching[0]


# --------------------------------------------------------------------------------------
# Everything matches: ready
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_all_checks_pass_when_everything_matches(preflight: N8nPreflight) -> None:
    result = preflight.check(make_entry())
    assert result.ready is True
    assert _check(result, "instance_reachable").status == "pass"
    assert _check(result, "workflow_exists").status == "pass"
    assert _check(result, "workflow_active").status == "pass"
    assert _check(result, "trigger_compatibility").status == "pass"
    assert _check(result, "definition_unchanged").status == "pass"
    assert _check(result, "credential_bindings").status == "pass"


# --------------------------------------------------------------------------------------
# AC-05: inactive workflow
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_ac05_inactive_workflow_fails_with_workflow_inactive(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, {**LIVE_DEFINITION, "active": False})
    result = preflight.check(make_entry())
    assert result.ready is False
    check = _check(result, "workflow_active")
    assert check.status == "fail"
    assert check.code == "WORKFLOW_INACTIVE"


# --------------------------------------------------------------------------------------
# AC-06: definition drift
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_ac06_definition_drift_reports_both_hashes(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    drifted = {
        **LIVE_DEFINITION,
        "nodes": [
            *LIVE_DEFINITION["nodes"],
            {"id": "new", "name": "New", "type": "x", "parameters": {}},
        ],
    }
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, drifted)
    entry = make_entry()  # definition_hash matches the ORIGINAL (undrifted) definition
    result = preflight.check(entry)
    assert result.ready is False
    check = _check(result, "definition_unchanged")
    assert check.status == "fail"
    assert check.code == "DEFINITION_DRIFT"
    assert check.detail["registered"] == entry.definition_hash
    assert check.detail["live"] == compute_definition_hash(drifted)
    assert check.detail["registered"] != check.detail["live"]


# --------------------------------------------------------------------------------------
# AC-07: instance unreachable
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_ac07_unreachable_instance_reports_instance_unreachable_and_skips_the_rest(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    mock_n8n.unreachable = True
    result = preflight.check(make_entry())
    assert result.ready is False
    assert _check(result, "instance_reachable").status == "fail"
    assert _check(result, "instance_reachable").code == "INSTANCE_UNREACHABLE"
    for name in (
        "compatible_version",
        "workflow_exists",
        "workflow_active",
        "trigger_compatibility",
        "definition_unchanged",
        "credential_bindings",
    ):
        assert _check(result, name).status == "skipped"


# --------------------------------------------------------------------------------------
# workflow missing on instance
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_missing_workflow_fails_and_skips_dependent_checks(preflight: N8nPreflight) -> None:
    result = preflight.check(make_entry(n8n_workflow_id="does-not-exist"))
    assert result.ready is False
    assert _check(result, "workflow_exists").status == "fail"
    assert _check(result, "workflow_exists").code == "WORKFLOW_MISSING_ON_INSTANCE"
    for name in (
        "workflow_active",
        "trigger_compatibility",
        "definition_unchanged",
        "credential_bindings",
    ):
        assert _check(result, name).status == "skipped"


# --------------------------------------------------------------------------------------
# Trigger compatibility
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_trigger_path_mismatch_fails(preflight: N8nPreflight) -> None:
    entry = make_entry(
        trigger=Trigger.model_construct(
            type="webhook", method="POST", path="/webhook/WRONG", auth="none", correlation="none"
        )
    )
    result = preflight.check(entry)
    check = _check(result, "trigger_compatibility")
    assert check.status == "fail"
    assert check.code == "TRIGGER_INCOMPATIBLE"


@pytest.mark.integration
def test_trigger_method_mismatch_fails(preflight: N8nPreflight) -> None:
    entry = make_entry(
        trigger=Trigger.model_construct(
            type="webhook",
            method="GET",
            path="/webhook/spike-test",
            auth="none",
            correlation="none",
        )
    )
    result = preflight.check(entry)
    assert _check(result, "trigger_compatibility").status == "fail"


@pytest.mark.integration
def test_no_webhook_node_on_live_definition_fails_trigger_compatibility(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, {**LIVE_DEFINITION, "nodes": []})
    result = preflight.check(make_entry())
    assert _check(result, "trigger_compatibility").status == "fail"


# --------------------------------------------------------------------------------------
# Credential bindings vs. validity — AC-30
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_credential_validity_is_always_unverifiable(preflight: N8nPreflight) -> None:
    result = preflight.check(make_entry())
    check = _check(result, "credential_validity")
    assert check.status == "unverifiable"
    assert check.code == "CREDENTIAL_VALIDITY_UNVERIFIED"


@pytest.mark.integration
def test_missing_credential_binding_is_reported_distinctly_from_unverifiable_validity(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    unbound = {
        **LIVE_DEFINITION,
        "nodes": [
            LIVE_DEFINITION["nodes"][0],
            {
                "id": "http-1",
                "name": "HTTP Call",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [200, 0],
                "parameters": {
                    "url": "https://example.invalid",
                    "authentication": "predefinedCredentialType",
                    "nodeCredentialType": "httpBasicAuth",
                },
                # no "credentials" key at all - required but unbound
            },
        ],
    }
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, unbound)
    entry = make_entry(definition_hash=compute_definition_hash(unbound))
    result = preflight.check(entry)
    bindings_check = _check(result, "credential_bindings")
    assert bindings_check.status == "fail"
    assert bindings_check.code == "MISSING_NODE_CREDENTIALS"
    assert bindings_check.detail["missing"][0]["node"] == "HTTP Call"
    # distinct from validity, which never claims anything beyond "cannot verify"
    validity_check = _check(result, "credential_validity")
    assert validity_check.status == "unverifiable"
    assert validity_check.code == "CREDENTIAL_VALIDITY_UNVERIFIED"


@pytest.mark.integration
def test_a_workflow_with_no_credentialed_nodes_passes_bindings(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    plain = {**LIVE_DEFINITION, "nodes": [LIVE_DEFINITION["nodes"][0]]}
    mock_n8n.add_workflow(N8N_WORKFLOW_ID, plain)
    entry = make_entry(definition_hash=compute_definition_hash(plain))
    result = preflight.check(entry)
    assert _check(result, "credential_bindings").status == "pass"


# --------------------------------------------------------------------------------------
# Correlation — AC-30
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_ac30_correlation_none_warns_and_stays_ready(preflight: N8nPreflight) -> None:
    entry = make_entry(
        trigger=Trigger.model_construct(
            type="webhook",
            method="POST",
            path="/webhook/spike-test",
            auth="none",
            correlation="none",
        )
    )
    result = preflight.check(entry)
    check = _check(result, "correlation")
    assert check.status == "warn"
    assert check.code == "NO_EXECUTION_CORRELATION"
    assert result.ready is True  # warn never blocks


@pytest.mark.integration
def test_correlation_response_envelope_passes(preflight: N8nPreflight) -> None:
    entry = make_entry(
        trigger=Trigger.model_construct(
            type="webhook",
            method="POST",
            path="/webhook/spike-test",
            auth="none",
            correlation="response_envelope",
        )
    )
    result = preflight.check(entry)
    assert _check(result, "correlation").status == "pass"


# --------------------------------------------------------------------------------------
# Unattended execution (ADR-009 section 5)
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_unattended_execution_warns_when_eligible_for_t05(preflight: N8nPreflight) -> None:
    entry = make_entry(approval="none", side_effects="read_only")
    result = preflight.check(entry)
    matching = [c for c in result.checks if c.check == "unattended_execution"]
    assert len(matching) == 1
    assert matching[0].status == "warn"
    assert matching[0].code == "UNATTENDED_EXECUTION"
    assert result.ready is True


@pytest.mark.integration
def test_no_unattended_execution_warning_when_approval_is_required(preflight: N8nPreflight) -> None:
    entry = make_entry(approval="required", side_effects="read_only")
    result = preflight.check(entry)
    assert [c for c in result.checks if c.check == "unattended_execution"] == []


# --------------------------------------------------------------------------------------
# compatible_version
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_compatible_version_passes_when_within_the_configured_set(preflight: N8nPreflight) -> None:
    result = preflight.check(make_entry())
    assert _check(result, "compatible_version").status == "pass"


@pytest.mark.integration
def test_compatible_version_warns_when_outside_the_configured_set(
    mock_n8n: MockN8n, client: N8nClient
) -> None:
    mock_n8n.api_version = "2.0.0"
    preflight = N8nPreflight(client, supported_api_versions=frozenset({"1.1.1"}))
    result = preflight.check(make_entry())
    check = _check(result, "compatible_version")
    assert check.status == "warn"
    assert check.code == "API_VERSION_UNVERIFIED"


@pytest.mark.integration
def test_compatible_version_is_unverifiable_with_no_configured_range(client: N8nClient) -> None:
    preflight = N8nPreflight(client, supported_api_versions=None)
    result = preflight.check(make_entry())
    check = _check(result, "compatible_version")
    assert check.status == "unverifiable"


# --------------------------------------------------------------------------------------
# Never PASS for something unavailable
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_no_check_is_ever_pass_when_its_prerequisite_failed(
    mock_n8n: MockN8n, preflight: N8nPreflight
) -> None:
    mock_n8n.unreachable = True
    result = preflight.check(make_entry())
    dependent = {
        "compatible_version",
        "workflow_exists",
        "workflow_active",
        "trigger_compatibility",
        "definition_unchanged",
        "credential_bindings",
    }
    for check in result.checks:
        if check.check in dependent:
            assert check.status != "pass"
