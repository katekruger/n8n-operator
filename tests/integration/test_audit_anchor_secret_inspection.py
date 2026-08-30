"""Secret/artifact inspection (stage 09, ADR-012 section 2, completion gate) — a
private key's raw bytes never appear in a stored ``audit_anchors.receipt``, and a
``ChainAnchor`` carries no audit-content field to leak in the first place (checked at
both the type-shape and instance level)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.audit_anchor.keys import generate_keypair, load_private_key, save_private_key
from n8n_operator.audit_anchor.local_file import LocalFileAnchor
from n8n_operator.core import service
from n8n_operator.core.models import ChainAnchor
from n8n_operator.storage.repository import AuditAnchorRepository, AuditLogRepository
from n8n_operator.storage.session import session_scope


@pytest.mark.integration
def test_private_key_bytes_never_appear_in_a_stored_receipt(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    private_bytes, _public_bytes = generate_keypair()
    key_path = tmp_path / "key"
    save_private_key(key_path, private_bytes)
    private_key = load_private_key(key_path)
    private_key_b64 = base64.b64encode(private_bytes).decode()

    from n8n_operator.audit.chain import compute_entry_hash

    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        occurred_at = datetime.now(UTC)
        entry_hash = compute_entry_hash(
            prev_hash=repo.get_last_hash(),
            occurred_at=occurred_at,
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="wf.a",
            outcome="allowed",
            detail={},
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash=entry_hash,
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="wf.a",
            outcome="allowed",
            occurred_at=occurred_at,
        )

    sink = LocalFileAnchor(path=tmp_path / "anchors.jsonl", private_key=private_key)

    class _Adapter:
        def publish(self, anchor: ChainAnchor) -> Any:
            raw = sink.publish(anchor)
            from n8n_operator.core.models import AnchorReceipt

            return AnchorReceipt(
                implementation=raw.implementation,  # type: ignore[arg-type]
                detail=raw.detail,
                signature=raw.signature,
                public_key=raw.public_key,
            )

        def verify(self, anchor: ChainAnchor, receipt: Any) -> Any:
            raise NotImplementedError

    with session_scope(session_factory) as session:
        service.publish_anchor(session, sink=_Adapter(), implementation="local_file")

    with session_scope(session_factory) as session:
        rows = AuditAnchorRepository(session).list_all(implementation="local_file")
    assert rows
    for row in rows:
        serialized = json.dumps(row.receipt)
        assert private_key_b64 not in serialized
        assert base64.b64encode(private_bytes).decode() not in serialized


def test_chain_anchor_has_no_field_that_could_carry_audit_content() -> None:
    """Static shape check: ``ChainAnchor``'s own field set is exactly the four
    ADR-012 names — no actor/subject/detail/argument field exists to accidentally
    populate, ever."""
    fields = set(ChainAnchor.model_fields.keys())
    assert fields == {"covers_through_seq", "entry_hash", "entry_count", "anchored_at"}


def test_anchor_canonical_bytes_payload_matches_only_chain_anchor_fields() -> None:
    from n8n_operator.audit_anchor.base import anchor_canonical_bytes

    anchor = ChainAnchor(
        covers_through_seq=1,
        entry_hash="sha256:" + "a" * 64,
        entry_count=1,
        anchored_at=datetime.now(UTC),
    )
    payload = json.loads(anchor_canonical_bytes(anchor))
    assert set(payload.keys()) == {"covers_through_seq", "entry_hash", "entry_count", "anchored_at"}
