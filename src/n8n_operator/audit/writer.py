"""The only module that computes a hash-chained audit entry.

The audit row commits in the same transaction as the state change it records. If the
audit write fails, the transition did not happen (invariant I6).

:func:`write` is the single writer abstraction this phase's task list asks for: every
audit-worthy event in the codebase — a state transition, a denied prepare attempt, a
registry reload — goes through this one function, so "compute the previous hash, hash
this entry's own content together with it, append" happens exactly once, correctly,
rather than being re-derived at each call site with a chance to diverge.

**No import from ``storage/``.** Like ``audit/chain.py``, this module cannot import
``storage.repository.AuditLogRepository`` — capability packages must not depend on each
other (ARCHITECTURE.md section 2.1) — despite existing entirely to produce a row that
ends up in ``storage``'s ``audit_log`` table. The resolution is dependency inversion: this
module is typed against a small structural :class:`AuditLogSink` protocol, and
``core/service.py`` (which *is* allowed to import both ``audit/`` and ``storage/``) passes
its own ``AuditLogRepository`` instance, which already satisfies the protocol's shape
without either module needing to know about the other's types.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from n8n_operator.audit.chain import compute_entry_hash

__all__ = ["AuditLogSink", "write"]


class AuditLogSink(Protocol):
    """What :func:`write` needs from a storage-layer audit repository.

    ``n8n_operator.storage.repository.AuditLogRepository`` already has exactly this
    shape — ``get_last_hash() -> str`` and ``append(...)`` with these keyword arguments —
    so no adapter class is needed to bridge the two; passing one directly satisfies this
    protocol structurally.
    """

    def get_last_hash(self) -> str: ...

    def append(
        self,
        *,
        prev_hash: str,
        entry_hash: str,
        actor: str,
        action: str,
        subject_type: str,
        subject_id: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> Any: ...


def write(
    sink: AuditLogSink,
    *,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> Any:
    """Append one hash-chained entry through ``sink``.

    Reads the current chain tip via ``sink.get_last_hash()`` (the genesis hash if the
    chain is empty), computes this entry's hash over its own canonical content plus that
    tip, and appends it — the read-tip/compute/append sequence happens inside whatever
    transaction the caller's ``sink`` is bound to, so it commits atomically with the state
    change it documents when the caller wraps both in one ``session_scope`` block
    (invariant I6). Returns whatever ``sink.append`` returns (the inserted row).
    """
    occurred_at = occurred_at or datetime.now(UTC)
    resolved_detail = detail or {}
    prev_hash = sink.get_last_hash()
    entry_hash = compute_entry_hash(
        prev_hash=prev_hash,
        occurred_at=occurred_at,
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        detail=resolved_detail,
    )
    return sink.append(
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        detail=resolved_detail,
        occurred_at=occurred_at,
    )
