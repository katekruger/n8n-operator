"""ADR-016 rules R13/R14 — overlay field allowlist and strengthen-only limits — plus
the real database's own `(workflow_id, environment_id)` uniqueness (AC-48).
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.registry.loader import (
    _LIMITS_STRENGTHEN_DIRECTION,
    _check_r13_overlay_field_allowlist,
    _check_r14_overlay_strengthen_only,
)
from n8n_operator.registry.schema import (
    Limits,
    Trigger,
    WorkflowEntry,
    WorkflowOverlayEntry,
)
from n8n_operator.storage.models import WorkflowEnvironmentOverlay
from n8n_operator.storage.repository import EnvironmentRepository, OrganizationRepository
from n8n_operator.storage.session import session_scope

_LOWER_ONLY_KEYS = tuple(k for k, d in _LIMITS_STRENGTHEN_DIRECTION.items() if d == "lower")
_RAISE_ONLY_KEYS = tuple(k for k, d in _LIMITS_STRENGTHEN_DIRECTION.items() if d == "raise")


def _base_entry(**limits_kwargs: Any) -> WorkflowEntry:
    return WorkflowEntry(
        id="wf.example",
        n8n_workflow_id="n8n-1",
        title="Example",
        description="d",
        owner="o",
        version=1,
        definition_hash="sha256:" + "a" * 64,
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=Trigger(type="webhook", method="POST", path="/webhook/a", auth="none"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        limits=Limits(**limits_kwargs),
    )


# ----------------------------------------------------------------------------------
# R14: strengthen-only limits — Hypothesis-generated base/overlay pairs.
# ----------------------------------------------------------------------------------


@given(
    key=st.sampled_from(_LOWER_ONLY_KEYS),
    base_value=st.integers(min_value=1, max_value=100_000),
    overlay_value=st.integers(min_value=1, max_value=100_000),
)
def test_r14_lower_only_keys_reject_any_raise(
    key: str, base_value: int, overlay_value: int
) -> None:
    """For a "lower is stricter" limit, an overlay may only ever lower it — any
    ``overlay_value > base_value`` is a violation, any ``overlay_value <= base_value``
    is not."""
    base = _base_entry(**{key: base_value})
    overlay = WorkflowOverlayEntry(workflow_id="wf.example", limits_override={key: overlay_value})
    violations = _check_r14_overlay_strengthen_only(base, overlay)
    if overlay_value > base_value:
        assert any(v.rule == "R14" for v in violations)
    else:
        assert not violations


@given(
    key=st.sampled_from(_RAISE_ONLY_KEYS),
    base_value=st.integers(min_value=1, max_value=100_000),
    overlay_value=st.integers(min_value=1, max_value=100_000),
)
def test_r14_raise_only_keys_reject_any_lower(
    key: str, base_value: int, overlay_value: int
) -> None:
    """The inverse for ``approval_ttl_seconds`` — the one limit where *more*
    deliberation time is the strict direction (ADR-016's own worked example)."""
    base = _base_entry(**{key: base_value})
    overlay = WorkflowOverlayEntry(workflow_id="wf.example", limits_override={key: overlay_value})
    violations = _check_r14_overlay_strengthen_only(base, overlay)
    if overlay_value < base_value:
        assert any(v.rule == "R14" for v in violations)
    else:
        assert not violations


_KEYS_WITH_NO_UNSET_DEFAULT = frozenset({"max_concurrent"})  # Limits.max_concurrent: int = 1


@given(
    key=st.sampled_from(
        [k for k in _LIMITS_STRENGTHEN_DIRECTION if k not in _KEYS_WITH_NO_UNSET_DEFAULT]
    ),
    overlay_value=st.integers(1, 1000),
)
def test_r14_no_base_ceiling_never_violates(key: str, overlay_value: int) -> None:
    """A base limit of ``None`` (no ceiling/floor configured) can never be violated by
    any overlay value — there is nothing to weaken. Excludes ``max_concurrent``, the
    one limit with a concrete, always-set default (``Limits.max_concurrent: int = 1``)
    rather than an unset ``None``."""
    base = _base_entry()  # every eligible limit field defaults to None
    overlay = WorkflowOverlayEntry(workflow_id="wf.example", limits_override={key: overlay_value})
    assert not _check_r14_overlay_strengthen_only(base, overlay)


# ----------------------------------------------------------------------------------
# R13: unknown fields, unknown workflow_id, duplicate entries.
# ----------------------------------------------------------------------------------


def test_r13_rejects_an_overlay_naming_a_workflow_id_not_in_the_base_registry() -> None:
    overlay = WorkflowOverlayEntry(workflow_id="does.not.exist", approval_override="required")
    violations = _check_r13_overlay_field_allowlist([overlay], base_ids={"wf.example"})
    assert any(v.rule == "R13" and v.workflow_id == "does.not.exist" for v in violations)


def test_r13_rejects_a_duplicate_workflow_id_within_one_overlay_document() -> None:
    overlays = [
        WorkflowOverlayEntry(workflow_id="wf.example", approval_override="required"),
        WorkflowOverlayEntry(workflow_id="wf.example", n8n_workflow_id="n8n-2"),
    ]
    violations = _check_r13_overlay_field_allowlist(overlays, base_ids={"wf.example"})
    assert any("duplicate" in v.message for v in violations)


@given(
    bad_key=st.text(min_size=1, max_size=20).filter(lambda s: s not in _LIMITS_STRENGTHEN_DIRECTION)
)
def test_r13_rejects_an_unknown_limits_override_key(bad_key: str) -> None:
    overlay = WorkflowOverlayEntry(workflow_id="wf.example", limits_override={bad_key: 1})
    violations = _check_r13_overlay_field_allowlist([overlay], base_ids={"wf.example"})
    assert any(v.rule == "R13" and "unknown limit key" in v.message for v in violations)


def test_overlay_entry_extra_fields_are_rejected_at_parse_time() -> None:
    """The fields R13 doesn't need to check at all, because ``extra="forbid"`` on the
    model itself already makes them unreachable — ``input_schema``/``side_effects``/
    ``risk``/``title``/``description``/``tags`` have no field on this model to even
    name."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, any shape
        WorkflowOverlayEntry(workflow_id="wf.example", risk="high")  # type: ignore[call-arg]


# ----------------------------------------------------------------------------------
# AC-48: the real database's own (workflow_id, environment_id) uniqueness.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_duplicate_workflow_environment_overlay_row_violates_the_db_constraint(
    session_factory: sessionmaker[Session],
) -> None:
    """``WorkflowEnvironmentOverlayRepository.upsert`` never reaches this path itself
    (it checks-then-updates) — this proves the constraint is real, independent of that
    repository's own discipline, the same "don't just trust the ORM layer" precedent
    the idempotency-namespace constraint test elsewhere in this suite already sets."""
    with session_scope(session_factory) as session:
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="staging",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        environment_id = env.id

    session = session_factory()
    try:
        session.add(
            WorkflowEnvironmentOverlay(workflow_id="wf.example", environment_id=environment_id)
        )
        session.flush()
        session.add(
            WorkflowEnvironmentOverlay(workflow_id="wf.example", environment_id=environment_id)
        )
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        session.close()
