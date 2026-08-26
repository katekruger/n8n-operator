"""Hash-chain construction and verification.

Each entry hashes the canonical serialization of its own fields together with the
previous entry's hash; genesis is 64 zeros. Verification walks a range and reports the
first break by sequence number (AC-22).

v2 adds the ``AuditAnchor`` interface -- content-free anchors published to a signed local
file or an authenticated HTTPS webhook -- so chain state is pinned somewhere an attacker
with database write access does not control (ADR-012, residual risk RR-4).

**No import from ``storage/``, ``registry/``, ``n8n/``, or ``core/``.** ``audit/`` is a
capability package and capability packages must not depend on each other or on ``core/``
(ARCHITECTURE.md section 2.1) — even though ``audit_log`` is a table defined in
``storage/models.py``. :func:`verify_chain` is therefore typed against a small structural
:class:`AuditEntryLike` protocol rather than the ORM's ``AuditLogEntry``, and this module
reimplements the same small canonical-JSON recipe ``registry/loader.py`` already defines
rather than importing it — eight duplicated lines are cheaper than a forbidden
cross-capability dependency the layering contract test would reject outright.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

GENESIS_HASH = "0" * 64
"""``audit_log.prev_hash`` for the first entry in the chain (BUILD_PLAN section 8.1).
Duplicated from ``storage.models.GENESIS_HASH`` rather than imported, for the same reason
every other name in this module avoids importing ``storage`` — a single literal is not
worth a forbidden cross-capability edge."""

__all__ = [
    "GENESIS_HASH",
    "AuditEntryLike",
    "ChainVerificationResult",
    "compute_entry_hash",
    "entry_canonical_bytes",
    "verify_chain",
]


def _canonical_json_bytes(value: Any) -> bytes:
    """The same recipe as ``registry.loader.canonical_json_bytes``: sorted keys, compact
    separators, no ASCII-escaping — reimplemented locally (see module docstring)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def entry_canonical_bytes(
    *,
    prev_hash: str,
    occurred_at: datetime,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    outcome: str,
    detail: dict[str, Any],
) -> bytes:
    """The exact bytes an entry's ``entry_hash`` is a sha256 digest of.

    ``occurred_at`` is serialized as an ISO 8601 string in UTC — a ``datetime`` is not
    itself JSON-serializable, and using ``isoformat()`` keeps the hash stable across a
    round trip through storage regardless of how a particular database driver represents
    timestamps (contrast ``storage.models.UTCDateTime``'s dialect-normalization concern,
    which this sidesteps entirely by hashing text, not a driver value).
    """
    aware_occurred_at = (
        occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=UTC)
    )
    payload = {
        "prev_hash": prev_hash,
        "occurred_at": aware_occurred_at.astimezone(UTC).isoformat(),
        "actor": actor,
        "action": action,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "outcome": outcome,
        "detail": detail,
    }
    return _canonical_json_bytes(payload)


def compute_entry_hash(
    *,
    prev_hash: str,
    occurred_at: datetime,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    outcome: str,
    detail: dict[str, Any],
) -> str:
    """``sha256:<hex>`` over :func:`entry_canonical_bytes` of the same fields."""
    digest = hashlib.sha256(
        entry_canonical_bytes(
            prev_hash=prev_hash,
            occurred_at=occurred_at,
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            outcome=outcome,
            detail=detail,
        )
    ).hexdigest()
    return f"sha256:{digest}"


class AuditEntryLike(Protocol):
    """Structural shape :func:`verify_chain` needs — satisfied by both
    ``storage.models.AuditLogEntry`` (an ORM row) and ``core.models.AuditEvent`` (a
    detached domain object) without importing either."""

    seq: int
    prev_hash: str
    entry_hash: str
    occurred_at: datetime
    actor: str
    action: str
    subject_type: str
    subject_id: str
    outcome: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class ChainVerificationResult:
    """The result of walking a range of entries in ``seq`` order."""

    ok: bool
    first_break_seq: int | None
    reason: str | None


def verify_chain(entries: Sequence[AuditEntryLike]) -> ChainVerificationResult:
    """Walk ``entries`` (already ordered by ``seq``, ascending) and confirm the chain.

    Two independent things can break at any entry: its ``prev_hash`` can fail to match
    the previous entry's ``entry_hash`` (a row inserted, deleted, or reordered), or its
    own ``entry_hash`` can fail to match a fresh recomputation over its stored fields (a
    row's content edited in place). Either failure is reported as "the chain breaks at
    this sequence number" — this is tamper-*evidence*: it names where verification first
    fails, not what changed or who changed it (BUILD_PLAN section 9.4).
    """
    expected_prev = GENESIS_HASH
    for entry in entries:
        if entry.prev_hash != expected_prev:
            return ChainVerificationResult(
                ok=False,
                first_break_seq=entry.seq,
                reason="prev_hash does not match the prior entry",
            )
        recomputed = compute_entry_hash(
            prev_hash=entry.prev_hash,
            occurred_at=entry.occurred_at,
            actor=entry.actor,
            action=entry.action,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            outcome=entry.outcome,
            detail=entry.detail,
        )
        if recomputed != entry.entry_hash:
            return ChainVerificationResult(
                ok=False,
                first_break_seq=entry.seq,
                reason="entry_hash does not match its own content",
            )
        expected_prev = entry.entry_hash
    return ChainVerificationResult(ok=True, first_break_seq=None, reason=None)
