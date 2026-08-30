"""``AuditAnchorRepository`` (stage 09, ADR-012 section 2) — pure storage-layer
behavior: create/get_latest/list_all, no policy (dedup/idempotency lives in
``core.service.publish_anchor``, tested separately)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.storage.repository import AuditAnchorRepository
from n8n_operator.storage.session import session_scope


@pytest.mark.integration
def test_create_and_get_latest(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        AuditAnchorRepository(session).create(
            covers_through_seq=10,
            entry_hash="sha256:aaa",
            implementation="local_file",
            receipt={"file_path": "/tmp/x", "line_number": 1},
        )
        latest = AuditAnchorRepository(session).get_latest(implementation="local_file")
        assert latest is not None
        assert latest.covers_through_seq == 10


@pytest.mark.integration
def test_get_latest_returns_the_most_recently_published(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditAnchorRepository(session)
        repo.create(
            covers_through_seq=10, entry_hash="sha256:aaa", implementation="local_file", receipt={}
        )
        repo.create(
            covers_through_seq=20, entry_hash="sha256:bbb", implementation="local_file", receipt={}
        )
        latest = repo.get_latest(implementation="local_file")
        assert latest is not None
        assert latest.covers_through_seq == 20


@pytest.mark.integration
def test_get_latest_successful_only_skips_failed_attempts(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditAnchorRepository(session)
        repo.create(
            covers_through_seq=10, entry_hash="sha256:aaa", implementation="local_file", receipt={}
        )
        repo.create(
            covers_through_seq=20,
            entry_hash="sha256:bbb",
            implementation="local_file",
            receipt={"error": "boom"},
            publish_failed=True,
        )
        latest_ok = repo.get_latest(implementation="local_file", successful_only=True)
        latest_any = repo.get_latest(implementation="local_file", successful_only=False)
    assert latest_ok is not None
    assert latest_ok.covers_through_seq == 10
    assert latest_any is not None
    assert latest_any.covers_through_seq == 20
    assert latest_any.publish_failed is True


@pytest.mark.integration
def test_get_latest_is_scoped_by_implementation(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        repo = AuditAnchorRepository(session)
        repo.create(
            covers_through_seq=10, entry_hash="sha256:aaa", implementation="local_file", receipt={}
        )
        repo.create(
            covers_through_seq=15,
            entry_hash="sha256:ccc",
            implementation="https_webhook",
            receipt={},
        )
        local = repo.get_latest(implementation="local_file")
        webhook = repo.get_latest(implementation="https_webhook")
    assert local is not None and local.covers_through_seq == 10
    assert webhook is not None and webhook.covers_through_seq == 15


@pytest.mark.integration
def test_get_latest_with_no_anchors_returns_none(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        latest = AuditAnchorRepository(session).get_latest(implementation="local_file")
    assert latest is None


@pytest.mark.integration
def test_list_all_returns_every_row_for_the_implementation(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditAnchorRepository(session)
        repo.create(
            covers_through_seq=20, entry_hash="sha256:bbb", implementation="local_file", receipt={}
        )
        repo.create(
            covers_through_seq=10, entry_hash="sha256:aaa", implementation="local_file", receipt={}
        )
        repo.create(
            covers_through_seq=15,
            entry_hash="sha256:ccc",
            implementation="https_webhook",
            receipt={},
        )
        rows = repo.list_all(implementation="local_file")
    assert {r.covers_through_seq for r in rows} == {10, 20}


@pytest.mark.integration
def test_implementation_check_constraint_rejects_unknown_values(
    session_factory: sessionmaker[Session],
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        AuditAnchorRepository(session).create(
            covers_through_seq=10,
            entry_hash="sha256:aaa",
            implementation="not_a_real_implementation",
            receipt={},
        )


@pytest.mark.integration
def test_no_update_or_delete_method_exists() -> None:
    """Boundary B11, extended to this table: append-only from Operator's own side
    (ADR-012's own explicit requirement)."""
    public_methods = {name for name in dir(AuditAnchorRepository) if not name.startswith("_")}
    assert public_methods == {"create", "get_latest", "list_all"}
