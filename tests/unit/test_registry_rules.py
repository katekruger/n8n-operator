"""One failing fixture per load-time rule R1 through R11, plus a direct-call test for
R12 (structurally unreachable via YAML in v1 — ``trigger.type`` is ``Literal["webhook"]``
only, so no document can ever set a different trigger type to violate it against).

Fixtures live under ``tests/fixtures/registry/`` and are generated to violate *exactly*
one rule each — verified here by asserting the exact violation set, not just "at least
one violation occurred".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from n8n_operator.registry.loader import RegistryValidationError, load_registry
from n8n_operator.registry.schema import Trigger, WorkflowEntry

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "registry"
EXAMPLE_REGISTRY = (
    Path(__file__).resolve().parents[2] / "examples" / "registry" / "workflows.example.yaml"
)

RULE_FIXTURES: dict[str, str] = {
    "R1": "r1_unsupported_api_version.yaml",
    "R2a": "r2_id_pattern.yaml",
    "R2b": "r2_duplicate_id.yaml",
    "R3": "r3_duplicate_n8n_workflow_id.yaml",
    "R4": "r4_input_schema_additional_properties.yaml",
    "R5": "r5_approval_none_requires_read_only.yaml",
    "R6": "r6_literal_secret.yaml",
    "R7": "r7_definition_hash_format.yaml",
    "R8": "r8_absolute_trigger_path.yaml",
    "R9": "r9_unparseable_redact_path.yaml",
    "R10": "r10_high_risk_requires_approval.yaml",
    "R11": "r11_max_argument_bytes_exceeds_ceiling.yaml",
}

# The rule each fixture is actually expected to violate (R2 has two distinct fixtures).
EXPECTED_RULE: dict[str, str] = {key: key.rstrip("ab") for key in RULE_FIXTURES}


@pytest.mark.unit
def test_every_declared_rule_fixture_file_exists() -> None:
    for name in RULE_FIXTURES.values():
        assert (FIXTURES_DIR / name).is_file(), name


@pytest.mark.unit
def test_fixtures_directory_has_no_undeclared_extra_files() -> None:
    """Every ``.yaml`` under the fixtures directory is accounted for above — catches a
    fixture added without a corresponding test entry."""
    on_disk = {p.name for p in FIXTURES_DIR.glob("*.yaml")}
    assert on_disk == set(RULE_FIXTURES.values())


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(RULE_FIXTURES.keys()))
def test_fixture_violates_exactly_its_named_rule(key: str) -> None:
    path = FIXTURES_DIR / RULE_FIXTURES[key]
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry(path, server_max_argument_bytes=262_144)
    violated_rules = {v.rule for v in excinfo.value.violations}
    assert violated_rules == {EXPECTED_RULE[key]}, (
        f"{path.name}: expected only {{{EXPECTED_RULE[key]}}}, got {violated_rules}"
    )


@pytest.mark.unit
def test_r12_is_checked_directly_since_it_is_unreachable_via_yaml_in_v1() -> None:
    """``trigger.type`` is ``Literal["webhook"]`` in v1 (schema.py); the R12 branch
    guarding a hypothetical non-webhook trigger can never fire through the public
    ``load_registry`` API today. Exercised directly via ``model_construct`` (which
    bypasses Pydantic's own Literal validation) to prove the check itself is correct
    and ready for the day a second trigger type exists."""
    from n8n_operator.registry.loader import _check_r12_correlation

    future_trigger = Trigger.model_construct(
        type="future_trigger_type",
        method="POST",
        path="/x",
        auth="none",
        correlation="response_envelope",
    )
    entry = WorkflowEntry.model_construct(
        id="x",
        n8n_workflow_id="y",
        title="t",
        description="d",
        owner="o",
        version=1,
        definition_hash="sha256:" + "a" * 64,
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=future_trigger,
        input_schema={},
        output=None,
        limits=None,
        tags=[],
        enabled=True,
    )
    violation = _check_r12_correlation(entry)
    assert violation is not None
    assert violation.rule == "R12"


@pytest.mark.unit
def test_r12_does_not_fire_for_the_only_trigger_type_v1_actually_supports() -> None:
    from n8n_operator.registry.loader import _check_r12_correlation

    webhook_trigger = Trigger.model_validate(
        {
            "type": "webhook",
            "method": "POST",
            "path": "/x",
            "auth": "none",
            "correlation": "response_envelope",
        }
    )
    entry = WorkflowEntry.model_construct(
        id="x",
        n8n_workflow_id="y",
        title="t",
        description="d",
        owner="o",
        version=1,
        definition_hash="sha256:" + "a" * 64,
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=webhook_trigger,
        input_schema={},
        output=None,
        limits=None,
        tags=[],
        enabled=True,
    )
    assert _check_r12_correlation(entry) is None


@pytest.mark.unit
def test_example_registry_loads_clean() -> None:
    """``examples/registry/workflows.example.yaml`` — the valid example referenced by
    README.md and WORKFLOW_REGISTRY.md — must load without any rule violation."""
    loaded = load_registry(EXAMPLE_REGISTRY, server_max_argument_bytes=262_144)
    assert len(loaded.entries) >= 1
    assert loaded.content_hash.startswith("sha256:")


@pytest.mark.unit
def test_example_registry_has_at_least_one_disabled_entry() -> None:
    """WORKFLOW_REGISTRY.md section 9.3 documents retiring a workflow via
    ``enabled: false`` rather than deletion; the example demonstrates it."""
    loaded = load_registry(EXAMPLE_REGISTRY, server_max_argument_bytes=262_144)
    assert any(not e.enabled for e in loaded.entries)


@pytest.mark.unit
def test_example_registry_demonstrates_every_side_effects_class() -> None:
    loaded = load_registry(EXAMPLE_REGISTRY, server_max_argument_bytes=262_144)
    classes = {e.side_effects for e in loaded.entries}
    assert classes == {"read_only", "external_write", "irreversible"}
