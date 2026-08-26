"""The operation state machine — the only place a transition is decided.

Twelve states and fifteen transitions (T01-T15), defined normatively in
``docs/BUILD_PLAN.md`` sections 5.1 and 5.2. Transitions are expressed as data so they
can be enumerated by property tests; no other module changes ``operations.state``.

Each transition emits exactly one ``operation_events`` row and one ``audit_log`` row in
the same transaction as the state change (invariant I6).

Lazy transactional expiry is authoritative: every read of, and action on, an operation
applies any overdue T08 or T11 here, in the same transaction, before state is evaluated.
Sweepers and ``operations expire`` improve audit-timeline fidelity, never safety
(invariant I9, ADR-010).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
