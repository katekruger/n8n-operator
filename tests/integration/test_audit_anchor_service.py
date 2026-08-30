"""``core.service.publish_anchor``/``get_anchor_status``/``verify_anchor_against_database``
(stage 09, ADR-012 section 2) — dedup/idempotency policy, fail-visible publish
failures, admin gating, and genuinely-independent-database verification, all against
a real database. The two concrete ``AuditAnchor`` implementations
(``audit_anchor.local_file``/``webhook``) are tested in isolation elsewhere; this file
uses a fake port so these tests are about the service-layer policy, not any one
implementation's own mechanics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import AnchorReceipt, ChainAnchor
from n8n_operator.errors import InsufficientRoleError
from n8n_operator.storage.repository import (
    AuditAnchorRepository,
    AuditLogRepository,
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)


class FakeSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.publish_calls: list[ChainAnchor] = []

    def publish(self, anchor: ChainAnchor) -> AnchorReceipt:
        self.publish_calls.append(anchor)
        if self.fail:
            raise RuntimeError("boom")
        return AnchorReceipt(
            implementation="local_file", detail={"line_number": 1}, signature="sig", public_key="pk"
        )

    def verify(self, anchor: ChainAnchor, receipt: AnchorReceipt) -> Any:
        raise NotImplementedError


def _write_audit_entries(session_factory: sessionmaker[Session], count: int) -> None:
    """Writes ``count`` real, correctly hash-chained entries — using
    ``audit.chain.compute_entry_hash``, the same function
    ``audit.writer.write`` uses in production, not a fake placeholder hash,
    since ``verify_chain`` (and therefore ``verify_anchor_against_database``)
    recomputes and checks each entry's own hash."""
    from datetime import UTC, datetime

    from n8n_operator.audit.chain import compute_entry_hash

    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        prev = repo.get_last_hash()
        for _ in range(count):
            occurred_at = datetime.now(UTC)
            entry_hash = compute_entry_hash(
                prev_hash=prev,
                occurred_at=occurred_at,
                actor="system",
                action="a",
                subject_type="workflow",
                subject_id="wf.a",
                outcome="allowed",
                detail={},
            )
            entry = repo.append(
                prev_hash=prev,
                entry_hash=entry_hash,
                actor="system",
                action="a",
                subject_type="workflow",
                subject_id="wf.a",
                outcome="allowed",
                occurred_at=occurred_at,
            )
            prev = entry.entry_hash


@pytest.mark.integration
def test_publish_anchor_on_an_empty_chain_is_a_well_defined_no_op(
    session_factory: sessionmaker[Session],
) -> None:
    sink = FakeSink()
    with session_scope(session_factory) as session:
        result = service.publish_anchor(session, sink=sink, implementation="local_file")
    assert result is None
    assert sink.publish_calls == []


@pytest.mark.integration
def test_publish_anchor_publishes_the_chain_tip(session_factory: sessionmaker[Session]) -> None:
    _write_audit_entries(session_factory, 3)
    sink = FakeSink()
    with session_scope(session_factory) as session:
        result = service.publish_anchor(session, sink=sink, implementation="local_file")
    assert result is not None
    assert result.covers_through_seq == 3
    assert result.publish_failed is False
    assert len(sink.publish_calls) == 1


@pytest.mark.integration
def test_publish_anchor_is_idempotent_when_nothing_new_exists(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 3)
    sink = FakeSink()
    with session_scope(session_factory) as session:
        service.publish_anchor(session, sink=sink, implementation="local_file")
    with session_scope(session_factory) as session:
        second = service.publish_anchor(session, sink=sink, implementation="local_file")
    assert second is not None
    assert second.covers_through_seq == 3
    # sink.publish was never called a second time — the whole point of the dedup.
    assert len(sink.publish_calls) == 1
    with session_scope(session_factory) as session:
        rows = AuditAnchorRepository(session).list_all(implementation="local_file")
    assert len(rows) == 1


@pytest.mark.integration
def test_publish_anchor_publishes_again_once_the_chain_grows(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 3)
    sink = FakeSink()
    with session_scope(session_factory) as session:
        service.publish_anchor(session, sink=sink, implementation="local_file")
    _write_audit_entries(session_factory, 2)
    with session_scope(session_factory) as session:
        second = service.publish_anchor(session, sink=sink, implementation="local_file")
    assert second is not None
    assert second.covers_through_seq == 5
    assert len(sink.publish_calls) == 2


@pytest.mark.integration
def test_publish_anchor_records_a_failed_attempt_visibly(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 1)
    sink = FakeSink(fail=True)
    with session_scope(session_factory) as session:
        result = service.publish_anchor(session, sink=sink, implementation="local_file")
    assert result is not None
    assert result.publish_failed is True
    with session_scope(session_factory) as session:
        rows = AuditAnchorRepository(session).list_all(implementation="local_file")
    assert len(rows) == 1
    assert rows[0].publish_failed is True


@pytest.mark.integration
def test_get_anchor_status_reports_gap_since_last_anchor(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 3)
    sink = FakeSink()
    with session_scope(session_factory) as session:
        service.publish_anchor(session, sink=sink, implementation="local_file")
    _write_audit_entries(session_factory, 4)
    with session_scope(session_factory) as session:
        summaries = service.get_anchor_status(session)
    assert len(summaries) == 1
    assert summaries[0].implementation == "local_file"
    assert summaries[0].last_covers_through_seq == 3
    assert summaries[0].chain_tip_seq == 7
    assert summaries[0].entries_since_last_anchor == 4


@pytest.mark.integration
def test_get_anchor_status_with_no_anchors_published_is_empty(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 3)
    with session_scope(session_factory) as session:
        summaries = service.get_anchor_status(session)
    assert summaries == []


@pytest.mark.integration
def test_publish_anchor_v2_admin_gated_viewer_denied(
    session_factory: sessionmaker[Session],
) -> None:
    _write_audit_entries(session_factory, 1)
    with session_scope(session_factory) as session:
        org = OrganizationRepository(session).create(name="Acme")
        EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id, organization_id=org.id, roles=["viewer"]
        )
        viewer_id = viewer.id

    sink = FakeSink()
    with (
        pytest.raises(InsufficientRoleError),
        session_scope(session_factory) as session,
    ):
        service.publish_anchor(
            session, sink=sink, implementation="local_file", principal_id=viewer_id, enable_v2=True
        )


@pytest.mark.integration
def test_verify_anchor_against_database_ok_on_a_matching_independent_copy(
    tmp_path: Path,
) -> None:
    copy_db_url = f"sqlite+pysqlite:///{tmp_path / 'copy.db'}"
    engine = create_engine_for_url(copy_db_url)
    from n8n_operator.storage.models import Base

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _write_audit_entries(session_factory, 3)
    with session_scope(session_factory) as session:
        tip = AuditLogRepository(session).get_last()
    assert tip is not None
    engine.dispose()

    result = service.verify_anchor_against_database(
        database_url=copy_db_url,
        covers_through_seq=tip.seq,
        entry_hash=tip.entry_hash,
        signature="unused-by-this-function",
        public_key="unused-by-this-function",
    )
    assert result.ok is True
    assert result.checked_through_seq == tip.seq


@pytest.mark.integration
def test_verify_anchor_against_database_fails_on_a_stale_copy(tmp_path: Path) -> None:
    copy_db_url = f"sqlite+pysqlite:///{tmp_path / 'stale.db'}"
    engine = create_engine_for_url(copy_db_url)
    from n8n_operator.storage.models import Base

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _write_audit_entries(session_factory, 2)  # copy only has 2 entries
    engine.dispose()

    result = service.verify_anchor_against_database(
        database_url=copy_db_url,
        covers_through_seq=5,  # the anchor claims 5
        entry_hash="sha256:whatever",
        signature="unused",
        public_key="unused",
    )
    assert result.ok is False
    assert result.reason is not None and "no entry at seq" in result.reason


@pytest.mark.integration
def test_verify_anchor_against_database_fails_on_a_mismatched_entry_hash(
    tmp_path: Path,
) -> None:
    copy_db_url = f"sqlite+pysqlite:///{tmp_path / 'mismatch.db'}"
    engine = create_engine_for_url(copy_db_url)
    from n8n_operator.storage.models import Base

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _write_audit_entries(session_factory, 3)
    engine.dispose()

    result = service.verify_anchor_against_database(
        database_url=copy_db_url,
        covers_through_seq=3,
        entry_hash="sha256:not-the-real-hash",
        signature="unused",
        public_key="unused",
    )
    assert result.ok is False
    assert result.reason is not None and "entry_hash" in result.reason


@pytest.mark.integration
def test_verify_anchor_against_database_never_touches_a_second_engine_beyond_the_copy(
    tmp_path: Path,
) -> None:
    """The function opens and disposes its own engine — confirmed here by simply
    calling it twice in a row against the same copy without leaking a connection
    (sqlite would raise/hang on a leaked exclusive lock long before this test's own
    timeout if the engine were never disposed)."""
    copy_db_url = f"sqlite+pysqlite:///{tmp_path / 'copy2.db'}"
    engine = create_engine_for_url(copy_db_url)
    from n8n_operator.storage.models import Base

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _write_audit_entries(session_factory, 1)
    with session_scope(session_factory) as session:
        tip = AuditLogRepository(session).get_last()
    assert tip is not None
    engine.dispose()

    for _ in range(3):
        result = service.verify_anchor_against_database(
            database_url=copy_db_url,
            covers_through_seq=tip.seq,
            entry_hash=tip.entry_hash,
            signature="unused",
            public_key="unused",
        )
        assert result.ok is True
