"""The operation state machine — the only place a transition is decided.

Twelve states and fifteen transitions (T01-T15), defined normatively in
``docs/BUILD_PLAN.md`` sections 5.1 and 5.2, expressed here as a plain data table rather
than as branching code, so a property test can enumerate every legal edge instead of
re-deriving it from behavior. No other module changes ``operations.state``:
``core/service.py`` calls :func:`validate_transition` before every
``OperationRepository.apply_transition`` call, and that repository method itself has no
notion of legality (``storage/repository.py``'s own docstring says so) — this module is
where "is this edge documented" is actually decided, once.

Each transition emits exactly one ``operation_events`` row and one ``audit_log`` row in
the same transaction as the state change (invariant I6) — enforced by
``core/service.py`` always pairing a transition with an ``audit/writer.py`` call inside
one ``session_scope`` block, not by anything in this module.

Lazy transactional expiry is authoritative: every read of, and action on, an operation
applies any overdue T08 or T11 here, in the same transaction, before state is evaluated.
Sweepers and ``operations expire`` improve audit-timeline fidelity, never safety
(invariant I9, ADR-010). :func:`overdue_expiry_transition` is what ``core/service.py``
calls first, unconditionally, at the top of every operation read or mutation.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from n8n_operator.errors import InvalidStateTransitionError
from n8n_operator.storage.models import STATES

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "TRANSITIONS_BY_ID",
    "Transition",
    "is_terminal",
    "overdue_expiry_transition",
    "validate_transition",
]


@dataclass(frozen=True)
class Transition:
    """One row of BUILD_PLAN section 5.2's table. ``from_state`` is ``None`` only for
    T01, the sole transition that creates a row rather than moving an existing one."""

    id: str
    from_state: str | None
    to_state: str


TRANSITIONS: tuple[Transition, ...] = (
    Transition("T01", None, "PREPARING"),
    Transition("T02", "PREPARING", "INVALID"),
    Transition("T03", "PREPARING", "BLOCKED"),
    Transition("T04", "PREPARING", "PENDING_APPROVAL"),
    Transition("T05", "PREPARING", "APPROVED"),
    Transition("T06", "PENDING_APPROVAL", "APPROVED"),
    Transition("T07", "PENDING_APPROVAL", "REJECTED"),
    Transition("T08", "PENDING_APPROVAL", "EXPIRED"),
    Transition("T09", "PENDING_APPROVAL", "CANCELED"),
    Transition("T10", "APPROVED", "EXECUTING"),
    Transition("T11", "APPROVED", "EXPIRED"),
    Transition("T12", "APPROVED", "CANCELED"),
    Transition("T13", "EXECUTING", "SUCCEEDED"),
    Transition("T14", "EXECUTING", "FAILED"),
    Transition("T15", "EXECUTING", "UNKNOWN"),
)
"""Exactly the fifteen edges in BUILD_PLAN section 5.2. There are no others: in
particular no edge out of any terminal state, no ``FAILED -> EXECUTING``, and no edge out
of ``UNKNOWN`` — it has no entry here as a ``from_state`` at all, so it is terminal by the
same mechanism every other terminal state is (absence from this table), not by a special
case (invariant I2, ``UNKNOWN`` remains terminal in v1)."""

TRANSITIONS_BY_ID: dict[str, Transition] = {t.id: t for t in TRANSITIONS}

_FROM_STATES: frozenset[str] = frozenset(
    t.from_state for t in TRANSITIONS if t.from_state is not None
)

TERMINAL_STATES: frozenset[str] = frozenset(STATES) - _FROM_STATES
"""A state is terminal iff no transition in the table ever leaves it — computed from the
table itself, not maintained as a second, separately-asserted list that could drift from
it. Matches BUILD_PLAN section 5.1's "terminal" column exactly: ``INVALID``, ``BLOCKED``,
``REJECTED``, ``EXPIRED``, ``CANCELED``, ``SUCCEEDED``, ``FAILED``, ``UNKNOWN``."""


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def validate_transition(transition_id: str, *, from_state: str | None) -> Transition:
    """The single gate every state change passes through.

    Raises :class:`~n8n_operator.errors.InvalidStateTransitionError` unless
    ``transition_id`` names a transition in :data:`TRANSITIONS` whose ``from_state``
    exactly matches the operation's current state — the only way an edge is "legal"
    (invariant I1). Because no row of the table names a terminal state as its
    ``from_state``, a caller can never construct a valid call out of one; terminal states
    are unreachable as a starting point by construction, not by an extra check here
    (invariant I2).
    """
    transition = TRANSITIONS_BY_ID.get(transition_id)
    if transition is None or transition.from_state != from_state:
        raise InvalidStateTransitionError(
            details={"transition": transition_id, "from_state": from_state}
        )
    return transition


def overdue_expiry_transition(
    *,
    state: str,
    now: datetime,
    approval_expires_at: datetime | None,
    execution_deadline: datetime | None,
) -> Transition | None:
    """T08 or T11 if ``state``'s deadline has passed as of ``now``, else ``None``.

    Called unconditionally at the top of every operation read or mutation, before state
    is evaluated for any other purpose (invariant I9, ADR-010) — the caller applies the
    returned transition (event + audit row, same transaction) before proceeding, so no
    caller ever observes or acts on a ``PENDING_APPROVAL``/``APPROVED`` operation whose
    deadline has already passed.
    """
    if (
        state == "PENDING_APPROVAL"
        and approval_expires_at is not None
        and now > approval_expires_at
    ):
        return TRANSITIONS_BY_ID["T08"]
    if state == "APPROVED" and execution_deadline is not None and now > execution_deadline:
        return TRANSITIONS_BY_ID["T11"]
    return None
