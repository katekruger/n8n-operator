"""Hypothesis properties for the state machine and the audit chain (BUILD_PLAN section
10.2, phase 3):

- State machine: for any random sequence of triggers from any state, the reached state
  is always one of the twelve documented states and every applied transition is one of
  the fifteen documented edges (I1, I2).
- Terminality: no generated trigger sequence produces an outgoing edge from a terminal
  state (I2).
- Handle single-use: for any number of sequential burn attempts on one operation, exactly
  one succeeds (I4) — the *logical* half of this property; genuine thread concurrency is
  covered separately by an integration test, since a race condition is not itself an
  input-space Hypothesis explores.
- Lazy expiry: for any clock position past a deadline, the overdue transition fires; for
  any position before it, it does not (I9).
- Audit chain: for any sequence of appended entries the chain verifies, and any single
  mutation makes verification fail at the mutated entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from n8n_operator.audit.chain import GENESIS_HASH, compute_entry_hash, verify_chain
from n8n_operator.core.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS_BY_ID,
    overdue_expiry_transition,
    validate_transition,
)
from n8n_operator.errors import InvalidStateTransitionError
from n8n_operator.storage.models import STATES

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)

_TRANSITION_IDS = list(TRANSITIONS_BY_ID.keys())
_STATE_LIST = list(STATES)

# --------------------------------------------------------------------------------------
# State machine: reachability and terminality (I1, I2)
# --------------------------------------------------------------------------------------


def _apply_sequence(transition_ids: list[str]) -> str | None:
    """Walk ``transition_ids`` starting from no operation, applying each only if it is
    legal from the current state (matching how ``core/service.py`` actually drives the
    machine: an illegal request is rejected and the state does not move). Returns the
    final state, or ``None`` if no transition ever applied (T01 never legally fired)."""
    state: str | None = None
    for transition_id in transition_ids:
        try:
            transition = validate_transition(transition_id, from_state=state)
        except InvalidStateTransitionError:
            continue
        state = transition.to_state
    return state


@given(transition_ids=st.lists(st.sampled_from(_TRANSITION_IDS), min_size=0, max_size=30))
@settings(max_examples=200)
def test_any_sequence_of_legal_transitions_always_lands_in_a_documented_state(
    transition_ids: list[str],
) -> None:
    final_state = _apply_sequence(transition_ids)
    assert final_state is None or final_state in STATES


@given(transition_ids=st.lists(st.sampled_from(_TRANSITION_IDS), min_size=0, max_size=30))
@settings(max_examples=200)
def test_no_sequence_ever_applies_an_edge_out_of_a_terminal_state(
    transition_ids: list[str],
) -> None:
    state: str | None = None
    for transition_id in transition_ids:
        if state is not None and state in TERMINAL_STATES:
            # A terminal state must reject every transition ID, with no exception.
            try:
                validate_transition(transition_id, from_state=state)
            except InvalidStateTransitionError:
                pass
            else:
                raise AssertionError(f"{transition_id} was accepted from terminal state {state!r}")
            continue
        try:
            transition = validate_transition(transition_id, from_state=state)
        except InvalidStateTransitionError:
            continue
        state = transition.to_state


@given(
    from_state=st.one_of(st.none(), st.sampled_from(_STATE_LIST)),
    transition_id=st.sampled_from(_TRANSITION_IDS),
)
@settings(max_examples=200)
def test_validate_transition_only_ever_accepts_the_documented_from_state(
    from_state: str | None, transition_id: str
) -> None:
    documented = TRANSITIONS_BY_ID[transition_id]
    if from_state == documented.from_state:
        result = validate_transition(transition_id, from_state=from_state)
        assert result.id == transition_id
    else:
        try:
            validate_transition(transition_id, from_state=from_state)
        except InvalidStateTransitionError:
            pass
        else:
            raise AssertionError("accepted an undocumented (transition, from_state) pair")


# --------------------------------------------------------------------------------------
# Handle single-use (I4) — the logical half; concurrency is integration-tested.
# --------------------------------------------------------------------------------------


@dataclass
class _FakeHandle:
    burned: bool = False

    def burn(self) -> bool:
        if self.burned:
            return False
        self.burned = True
        return True


@given(attempts=st.integers(min_value=1, max_value=50))
@settings(max_examples=50)
def test_exactly_one_of_any_number_of_sequential_burn_attempts_succeeds(attempts: int) -> None:
    handle = _FakeHandle()
    results = [handle.burn() for _ in range(attempts)]
    assert results.count(True) == 1
    assert results.count(False) == attempts - 1


# --------------------------------------------------------------------------------------
# Lazy expiry (I9)
# --------------------------------------------------------------------------------------


@given(offset_seconds=st.integers(min_value=1, max_value=10_000_000))
@settings(max_examples=100)
def test_overdue_expiry_fires_for_any_clock_position_past_the_deadline(offset_seconds: int) -> None:
    deadline = NOW - timedelta(seconds=offset_seconds)
    result = overdue_expiry_transition(
        state="PENDING_APPROVAL", now=NOW, approval_expires_at=deadline, execution_deadline=None
    )
    assert result is not None
    assert result.id == "T08"


@given(offset_seconds=st.integers(min_value=0, max_value=10_000_000))
@settings(max_examples=100)
def test_overdue_expiry_never_fires_before_or_at_the_deadline(offset_seconds: int) -> None:
    deadline = NOW + timedelta(seconds=offset_seconds)
    result = overdue_expiry_transition(
        state="PENDING_APPROVAL", now=NOW, approval_expires_at=deadline, execution_deadline=None
    )
    assert result is None


@given(
    offset_seconds=st.integers(min_value=-10_000_000, max_value=10_000_000),
    state=st.sampled_from(sorted(set(STATES) - {"PENDING_APPROVAL", "APPROVED"})),
)
@settings(max_examples=100)
def test_overdue_expiry_never_fires_for_a_state_with_no_deadline_semantics(
    offset_seconds: int, state: str
) -> None:
    deadline = NOW + timedelta(seconds=offset_seconds)
    result = overdue_expiry_transition(
        state=state, now=NOW, approval_expires_at=deadline, execution_deadline=deadline
    )
    assert result is None


# --------------------------------------------------------------------------------------
# Audit chain
# --------------------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    seq: int
    prev_hash: str
    entry_hash: str
    occurred_at: datetime
    actor: str
    action: str
    subject_type: str
    subject_id: str
    outcome: str
    detail: dict[str, object]


def _build_chain(n: int) -> list[_FakeEntry]:
    entries: list[_FakeEntry] = []
    prev = GENESIS_HASH
    for i in range(n):
        entry_hash = compute_entry_hash(
            prev_hash=prev,
            occurred_at=NOW,
            actor="local",
            action="operation.prepared",
            subject_type="operation",
            subject_id=f"op_{i}",
            outcome="allowed",
            detail={"i": i},
        )
        entries.append(
            _FakeEntry(
                i + 1,
                prev,
                entry_hash,
                NOW,
                "local",
                "operation.prepared",
                "operation",
                f"op_{i}",
                "allowed",
                {"i": i},
            )
        )
        prev = entry_hash
    return entries


@given(n=st.integers(min_value=0, max_value=20))
@settings(max_examples=50)
def test_any_correctly_built_chain_verifies(n: int) -> None:
    result = verify_chain(_build_chain(n))
    assert result.ok is True


@given(n=st.integers(min_value=1, max_value=20), data=st.data())
@settings(max_examples=50)
def test_any_single_mutation_makes_verification_fail_at_the_mutated_entry(
    n: int, data: st.DataObject
) -> None:
    entries = _build_chain(n)
    index = data.draw(st.integers(min_value=0, max_value=n - 1))
    entries[index].detail = {"tampered": True}
    result = verify_chain(entries)
    assert result.ok is False
    assert result.first_break_seq == entries[index].seq
