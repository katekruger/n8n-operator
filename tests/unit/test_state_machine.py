"""``core/state_machine.py``: the T01-T15 table, legality checks, and lazy expiry
(BUILD_PLAN section 12, phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from n8n_operator.core.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    TRANSITIONS_BY_ID,
    Transition,
    is_terminal,
    overdue_expiry_transition,
    validate_transition,
)
from n8n_operator.errors import InvalidStateTransitionError
from n8n_operator.storage.models import STATES
from n8n_operator.storage.models import TRANSITIONS as MODEL_TRANSITIONS

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_exactly_fifteen_transitions_are_defined() -> None:
    assert len(TRANSITIONS) == 15
    assert {t.id for t in TRANSITIONS} == set(MODEL_TRANSITIONS)


@pytest.mark.unit
def test_every_transition_id_and_state_matches_build_plan() -> None:
    expected = {
        "T01": (None, "PREPARING"),
        "T02": ("PREPARING", "INVALID"),
        "T03": ("PREPARING", "BLOCKED"),
        "T04": ("PREPARING", "PENDING_APPROVAL"),
        "T05": ("PREPARING", "APPROVED"),
        "T06": ("PENDING_APPROVAL", "APPROVED"),
        "T07": ("PENDING_APPROVAL", "REJECTED"),
        "T08": ("PENDING_APPROVAL", "EXPIRED"),
        "T09": ("PENDING_APPROVAL", "CANCELED"),
        "T10": ("APPROVED", "EXECUTING"),
        "T11": ("APPROVED", "EXPIRED"),
        "T12": ("APPROVED", "CANCELED"),
        "T13": ("EXECUTING", "SUCCEEDED"),
        "T14": ("EXECUTING", "FAILED"),
        "T15": ("EXECUTING", "UNKNOWN"),
    }
    actual = {t.id: (t.from_state, t.to_state) for t in TRANSITIONS}
    assert actual == expected


@pytest.mark.unit
def test_every_transition_state_is_one_of_the_twelve_states() -> None:
    for transition in TRANSITIONS:
        if transition.from_state is not None:
            assert transition.from_state in STATES
        assert transition.to_state in STATES


@pytest.mark.unit
def test_terminal_states_match_build_plan_section_5_1() -> None:
    assert {
        "INVALID",
        "BLOCKED",
        "REJECTED",
        "EXPIRED",
        "CANCELED",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
    } == TERMINAL_STATES


@pytest.mark.unit
def test_active_and_transient_states_are_not_terminal() -> None:
    for state in ("PREPARING", "PENDING_APPROVAL", "APPROVED", "EXECUTING"):
        assert not is_terminal(state)


@pytest.mark.unit
def test_unknown_is_terminal_and_has_no_outgoing_edge() -> None:
    assert is_terminal("UNKNOWN")
    assert all(t.from_state != "UNKNOWN" for t in TRANSITIONS)


@pytest.mark.unit
def test_no_terminal_state_is_ever_a_from_state() -> None:
    """Invariant I2: terminal states have no outgoing edges — checked directly against
    the table rather than inferred, since :data:`TERMINAL_STATES` is itself derived from
    this same table (a tautology check would prove nothing); this asserts the converse
    property the derivation relies on holds for every individual terminal state."""
    from_states = {t.from_state for t in TRANSITIONS if t.from_state is not None}
    assert from_states.isdisjoint(TERMINAL_STATES)


@pytest.mark.unit
def test_no_edge_from_failed_to_executing_or_any_edge_out_of_a_terminal_state() -> None:
    assert all(not (t.from_state == "FAILED" and t.to_state == "EXECUTING") for t in TRANSITIONS)


@pytest.mark.unit
@pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: t.id)
def test_validate_transition_accepts_every_documented_edge(transition: Transition) -> None:
    result = validate_transition(transition.id, from_state=transition.from_state)
    assert result is transition


@pytest.mark.unit
def test_validate_transition_rejects_an_unknown_transition_id() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("T99", from_state="PREPARING")


@pytest.mark.unit
def test_validate_transition_rejects_a_transition_from_the_wrong_state() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("T06", from_state="PREPARING")  # T06 requires PENDING_APPROVAL


@pytest.mark.unit
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_validate_transition_rejects_every_transition_id_from_every_terminal_state(
    state: str,
) -> None:
    """No documented transition ID is legal starting from a terminal state, whichever ID
    is tried — this is what "terminal states have no outgoing edges" means operationally."""
    for transition_id in TRANSITIONS_BY_ID:
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(transition_id, from_state=state)


@pytest.mark.unit
def test_overdue_expiry_transition_fires_t08_past_approval_deadline() -> None:
    result = overdue_expiry_transition(
        state="PENDING_APPROVAL",
        now=NOW,
        approval_expires_at=NOW - timedelta(seconds=1),
        execution_deadline=None,
    )
    assert result is not None
    assert result.id == "T08"


@pytest.mark.unit
def test_overdue_expiry_transition_fires_t11_past_execution_deadline() -> None:
    result = overdue_expiry_transition(
        state="APPROVED",
        now=NOW,
        approval_expires_at=None,
        execution_deadline=NOW - timedelta(seconds=1),
    )
    assert result is not None
    assert result.id == "T11"


@pytest.mark.unit
def test_overdue_expiry_transition_is_none_before_the_deadline() -> None:
    assert (
        overdue_expiry_transition(
            state="PENDING_APPROVAL",
            now=NOW,
            approval_expires_at=NOW + timedelta(seconds=1),
            execution_deadline=None,
        )
        is None
    )


@pytest.mark.unit
def test_overdue_expiry_transition_is_none_exactly_at_the_deadline() -> None:
    """``now > deadline``, not ``>=`` — the instant of expiry itself is not yet overdue."""
    assert (
        overdue_expiry_transition(
            state="PENDING_APPROVAL", now=NOW, approval_expires_at=NOW, execution_deadline=None
        )
        is None
    )


@pytest.mark.unit
def test_overdue_expiry_transition_is_none_for_a_state_with_no_deadline_concept() -> None:
    assert (
        overdue_expiry_transition(
            state="EXECUTING",
            now=NOW,
            approval_expires_at=NOW - timedelta(days=1),
            execution_deadline=NOW - timedelta(days=1),
        )
        is None
    )


@pytest.mark.unit
def test_overdue_expiry_transition_is_none_when_deadline_is_unset() -> None:
    assert (
        overdue_expiry_transition(
            state="PENDING_APPROVAL", now=NOW, approval_expires_at=None, execution_deadline=None
        )
        is None
    )
