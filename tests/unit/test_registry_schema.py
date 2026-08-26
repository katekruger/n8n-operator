"""Pydantic registry models: construction, defaults, resolution, response shaping
(BUILD_PLAN section 12, phase 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from n8n_operator.registry.schema import (
    Limits,
    RegistryDefaults,
    RegistryDocument,
    Trigger,
    WorkflowDetail,
    WorkflowEntry,
    WorkflowSummary,
    resolve_workflow_entry,
)


def _minimal_entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "wf.a",
        "n8n_workflow_id": "n8n-1",
        "title": "A",
        "description": "B",
        "owner": "carolyn",
        "version": 1,
        "definition_hash": "sha256:" + "a" * 64,
        "risk": "low",
        "side_effects": "read_only",
        "trigger": {"type": "webhook", "method": "POST", "path": "/webhook/a", "auth": "none"},
        "input_schema": {"type": "object", "additionalProperties": False},
    }
    base.update(overrides)
    return base


def _minimal_document(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "apiVersion": "n8n-operator/v1",
        "metadata": {"name": "test"},
        "workflows": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# apiVersion alias
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_api_version_parses_from_camel_case_alias() -> None:
    doc = RegistryDocument.model_validate(_minimal_document())
    assert doc.api_version == "n8n-operator/v1"


@pytest.mark.unit
def test_api_version_also_accepts_the_python_field_name() -> None:
    doc = RegistryDocument(api_version="n8n-operator/v1", metadata={"name": "t"})  # type: ignore[call-arg,arg-type]
    assert doc.api_version == "n8n-operator/v1"


# --------------------------------------------------------------------------------------
# Defaults and required fields
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_defaults_match_build_plan() -> None:
    defaults = RegistryDefaults()
    assert defaults.approval == "required"
    assert defaults.timeout_seconds == 60
    assert defaults.approval_ttl_seconds == 900
    assert defaults.execution_ttl_seconds == 300


@pytest.mark.unit
def test_workflow_entry_defaults() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    assert entry.approval is None  # not yet resolved
    assert entry.output.redact == []
    assert entry.output.max_bytes == 65536
    assert entry.output.include_node_trace is False
    assert entry.limits.max_concurrent == 1
    assert entry.limits.rate_limit_per_minute is None
    assert entry.limits.max_argument_bytes is None
    assert entry.tags == []
    assert entry.enabled is True


@pytest.mark.unit
def test_trigger_correlation_defaults_to_none() -> None:
    trigger = Trigger.model_validate(
        {"type": "webhook", "method": "POST", "path": "/x", "auth": "none"}
    )
    assert trigger.correlation == "none"


@pytest.mark.unit
@pytest.mark.parametrize("missing_field", ["id", "n8n_workflow_id", "title", "owner", "trigger"])
def test_missing_required_field_fails(missing_field: str) -> None:
    entry = _minimal_entry()
    del entry[missing_field]
    with pytest.raises(ValidationError):
        WorkflowEntry.model_validate(entry)


@pytest.mark.unit
def test_unknown_field_is_rejected() -> None:
    entry = _minimal_entry(unexpected_field="should not be allowed")
    with pytest.raises(ValidationError):
        WorkflowEntry.model_validate(entry)


@pytest.mark.unit
@pytest.mark.parametrize("field,bad_value", [("risk", "extreme"), ("side_effects", "maybe")])
def test_invalid_enum_value_rejected(field: str, bad_value: str) -> None:
    entry = _minimal_entry(**{field: bad_value})
    with pytest.raises(ValidationError):
        WorkflowEntry.model_validate(entry)


# --------------------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_entry_is_frozen() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    with pytest.raises(ValidationError):
        entry.title = "changed"


@pytest.mark.unit
def test_registry_document_is_frozen() -> None:
    doc = RegistryDocument.model_validate(_minimal_document())
    with pytest.raises(ValidationError):
        doc.workflows = []


# --------------------------------------------------------------------------------------
# resolve_workflow_entry
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_fills_unset_approval_from_defaults() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    defaults = RegistryDefaults(approval="required")
    resolved = resolve_workflow_entry(entry, defaults)
    assert resolved.approval == "required"


@pytest.mark.unit
def test_resolve_leaves_an_explicit_approval_untouched() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry(approval="none"))
    defaults = RegistryDefaults(approval="required")
    resolved = resolve_workflow_entry(entry, defaults)
    assert resolved.approval == "none"


@pytest.mark.unit
def test_resolve_fills_unset_limits_from_defaults() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    defaults = RegistryDefaults(
        timeout_seconds=45, approval_ttl_seconds=111, execution_ttl_seconds=222
    )
    resolved = resolve_workflow_entry(entry, defaults)
    assert resolved.limits.timeout_seconds == 45
    assert resolved.limits.approval_ttl_seconds == 111
    assert resolved.limits.execution_ttl_seconds == 222


@pytest.mark.unit
def test_resolve_leaves_explicit_limits_untouched() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry(limits={"timeout_seconds": 5}))
    defaults = RegistryDefaults(timeout_seconds=999)
    resolved = resolve_workflow_entry(entry, defaults)
    assert resolved.limits.timeout_seconds == 5


@pytest.mark.unit
def test_resolve_does_not_touch_fields_with_no_defaults_equivalent() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry(limits={"max_concurrent": 7}))
    resolved = resolve_workflow_entry(entry, RegistryDefaults())
    assert resolved.limits.max_concurrent == 7
    assert resolved.limits.max_argument_bytes is None
    assert resolved.limits.rate_limit_per_minute is None


@pytest.mark.unit
def test_resolve_returns_a_new_instance_without_mutating_the_original() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    resolved = resolve_workflow_entry(entry, RegistryDefaults(approval="required"))
    assert entry.approval is None  # untouched
    assert resolved.approval == "required"
    assert resolved is not entry


# --------------------------------------------------------------------------------------
# Public response shaping (boundary B5) — the field-level guarantee
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_summary_has_no_field_capable_of_carrying_sensitive_data() -> None:
    forbidden_field_names = {"n8n_workflow_id", "trigger", "secret_ref", "url", "input_schema"}
    assert set(WorkflowSummary.model_fields) & forbidden_field_names == set()


@pytest.mark.unit
def test_workflow_detail_has_no_field_capable_of_carrying_sensitive_data() -> None:
    forbidden_field_names = {"n8n_workflow_id", "trigger", "secret_ref", "url"}
    assert set(WorkflowDetail.model_fields) & forbidden_field_names == set()


@pytest.mark.unit
def test_workflow_summary_from_entry_requires_a_resolved_entry() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())  # approval is None — unresolved
    with pytest.raises(ValueError, match="resolved"):
        WorkflowSummary.from_entry(entry)


@pytest.mark.unit
def test_workflow_detail_from_entry_requires_a_resolved_entry() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry())
    with pytest.raises(ValueError, match="resolved"):
        WorkflowDetail.from_entry(entry)


@pytest.mark.unit
def test_workflow_summary_projects_the_documented_fields() -> None:
    entry = WorkflowEntry.model_validate(_minimal_entry(approval="none", tags=["a", "b"]))
    resolved = resolve_workflow_entry(entry, RegistryDefaults())
    summary = WorkflowSummary.from_entry(resolved)
    assert summary.workflow_id == "wf.a"
    assert summary.title == "A"
    assert summary.risk == "low"
    assert summary.side_effects == "read_only"
    assert summary.approval == "none"
    assert summary.tags == ["a", "b"]
    assert summary.owner == "carolyn"
    assert summary.version == 1


@pytest.mark.unit
def test_workflow_detail_redacted_paths_is_a_count_not_the_paths() -> None:
    entry = WorkflowEntry.model_validate(
        _minimal_entry(approval="none", output={"redact": ["$.a", "$.b", "$.c"]})
    )
    resolved = resolve_workflow_entry(entry, RegistryDefaults())
    detail = WorkflowDetail.from_entry(resolved)
    assert detail.output.redacted_paths == 3
    # The actual paths never appear anywhere in the serialized detail.
    dumped = detail.model_dump_json()
    assert "$.a" not in dumped and "$.b" not in dumped and "$.c" not in dumped


@pytest.mark.unit
def test_workflow_detail_includes_max_argument_bytes() -> None:
    """Phase-2 documentation fix: MCP_TOOLS.md's describe_workflow example predates
    ADR-011's addition of limits.max_argument_bytes; WorkflowDetail carries it."""
    entry = WorkflowEntry.model_validate(
        _minimal_entry(approval="none", limits={"max_argument_bytes": 4096})
    )
    resolved = resolve_workflow_entry(entry, RegistryDefaults())
    detail = WorkflowDetail.from_entry(resolved)
    assert detail.limits.max_argument_bytes == 4096


@pytest.mark.unit
def test_workflow_detail_serialization_never_contains_the_n8n_id_or_secret_ref() -> None:
    entry = WorkflowEntry.model_validate(
        _minimal_entry(
            approval="none",
            n8n_workflow_id="TOP-SECRET-N8N-ID-999",
            trigger={
                "type": "webhook",
                "method": "POST",
                "path": "/x",
                "auth": "header",
                "secret_ref": "env:SOME_TOKEN_NAME",
            },
        )
    )
    resolved = resolve_workflow_entry(entry, RegistryDefaults())
    dumped = WorkflowDetail.from_entry(resolved).model_dump_json()
    assert "TOP-SECRET-N8N-ID-999" not in dumped
    assert "SOME_TOKEN_NAME" not in dumped


@pytest.mark.unit
def test_limits_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Limits.model_validate({"not_a_real_field": 1})
