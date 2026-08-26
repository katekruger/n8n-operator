"""``audit/chain.py``: canonical serialization, hashing, and chain verification
(BUILD_PLAN section 12, phase 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from n8n_operator.audit.chain import GENESIS_HASH, compute_entry_hash, verify_chain

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


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


def _make_chain(n: int) -> list[_FakeEntry]:
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
                seq=i + 1,
                prev_hash=prev,
                entry_hash=entry_hash,
                occurred_at=NOW,
                actor="local",
                action="operation.prepared",
                subject_type="operation",
                subject_id=f"op_{i}",
                outcome="allowed",
                detail={"i": i},
            )
        )
        prev = entry_hash
    return entries


@pytest.mark.unit
def test_compute_entry_hash_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "prev_hash": GENESIS_HASH,
        "occurred_at": NOW,
        "actor": "local",
        "action": "operation.prepared",
        "subject_type": "operation",
        "subject_id": "op_1",
        "outcome": "allowed",
        "detail": {"a": 1},
    }
    assert compute_entry_hash(**kwargs) == compute_entry_hash(**kwargs)


@pytest.mark.unit
def test_compute_entry_hash_has_the_sha256_prefix_and_64_hex_chars() -> None:
    digest = compute_entry_hash(
        prev_hash=GENESIS_HASH,
        occurred_at=NOW,
        actor="local",
        action="operation.prepared",
        subject_type="operation",
        subject_id="op_1",
        outcome="allowed",
        detail={},
    )
    assert digest.startswith("sha256:")
    hex_part = digest.removeprefix("sha256:")
    assert len(hex_part) == 64
    int(hex_part, 16)


@pytest.mark.unit
def test_compute_entry_hash_changes_when_any_field_changes() -> None:
    base: dict[str, Any] = {
        "prev_hash": GENESIS_HASH,
        "occurred_at": NOW,
        "actor": "local",
        "action": "operation.prepared",
        "subject_type": "operation",
        "subject_id": "op_1",
        "outcome": "allowed",
        "detail": {"a": 1},
    }
    baseline = compute_entry_hash(**base)
    for field, new_value in [
        ("prev_hash", "sha256:" + "1" * 64),
        ("actor", "other"),
        ("action", "operation.rejected"),
        ("subject_type", "workflow"),
        ("subject_id", "op_2"),
        ("outcome", "denied"),
        ("detail", {"a": 2}),
    ]:
        mutated = dict(base)
        mutated[field] = new_value
        assert compute_entry_hash(**mutated) != baseline, (
            f"changing {field} did not change the hash"
        )


@pytest.mark.unit
def test_compute_entry_hash_is_insensitive_to_detail_key_order() -> None:
    a = compute_entry_hash(
        prev_hash=GENESIS_HASH,
        occurred_at=NOW,
        actor="local",
        action="x",
        subject_type="operation",
        subject_id="op_1",
        outcome="allowed",
        detail={"a": 1, "b": 2},
    )
    b = compute_entry_hash(
        prev_hash=GENESIS_HASH,
        occurred_at=NOW,
        actor="local",
        action="x",
        subject_type="operation",
        subject_id="op_1",
        outcome="allowed",
        detail={"b": 2, "a": 1},
    )
    assert a == b


@pytest.mark.unit
def test_verify_chain_accepts_an_empty_chain() -> None:
    result = verify_chain([])
    assert result.ok is True


@pytest.mark.unit
def test_verify_chain_accepts_a_correctly_built_chain() -> None:
    result = verify_chain(_make_chain(5))
    assert result.ok is True
    assert result.first_break_seq is None


@pytest.mark.unit
def test_verify_chain_rejects_a_bad_genesis_prev_hash() -> None:
    entries = _make_chain(3)
    entries[0].prev_hash = "sha256:" + "9" * 64
    result = verify_chain(entries)
    assert result.ok is False
    assert result.first_break_seq == 1


@pytest.mark.unit
def test_verify_chain_detects_content_tampering_at_the_exact_entry() -> None:
    entries = _make_chain(5)
    entries[2].detail = {"tampered": True}
    result = verify_chain(entries)
    assert result.ok is False
    assert result.first_break_seq == 3  # entries[2].seq == 3


@pytest.mark.unit
def test_verify_chain_detects_a_reordered_entry() -> None:
    entries = _make_chain(4)
    entries[1], entries[2] = entries[2], entries[1]
    result = verify_chain(entries)
    assert result.ok is False


@pytest.mark.unit
def test_verify_chain_reports_the_first_break_not_a_later_one() -> None:
    entries = _make_chain(5)
    entries[1].detail = {"tampered": True}
    entries[3].detail = {"also tampered": True}
    result = verify_chain(entries)
    assert result.ok is False
    assert result.first_break_seq == 2  # entries[1].seq
