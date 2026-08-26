"""``core.service.reload_registry`` / ``get_active_snapshot`` against a real database
(BUILD_PLAN section 12, phase 2).

Uses the ``session_factory`` fixture from ``tests/conftest.py`` — a real, file-based
SQLite database with the full migrated schema, matching what production actually runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core.service import get_active_snapshot, reload_registry
from n8n_operator.registry.loader import RegistryValidationError
from n8n_operator.storage.repository import WorkflowBindingRepository
from n8n_operator.storage.session import session_scope

VALID_DOCUMENT = """apiVersion: n8n-operator/v1
metadata:
  name: test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: A
    description: B
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      additionalProperties: false
""".format(hash="a" * 64)

INVALID_DOCUMENT = "apiVersion: n8n-operator/v99\nmetadata:\n  name: bad\nworkflows: []\n"


def _write(tmp_path: Path, text: str, name: str = "workflows.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


@pytest.mark.integration
def test_get_active_snapshot_is_none_before_any_reload(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        assert get_active_snapshot(session) is None


@pytest.mark.integration
def test_reload_creates_a_snapshot_and_bindings(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        snapshot, reused = reload_registry(session, path, server_max_argument_bytes=262_144)
        assert reused is False
        assert snapshot.content_hash.startswith("sha256:")

    with session_scope(session_factory) as session:
        active = get_active_snapshot(session)
        assert active is not None
        assert active.id == snapshot.id
        binding = WorkflowBindingRepository(session).get_by_snapshot_and_workflow_id(
            active.id, "wf.a"
        )
        assert binding is not None
        assert binding.n8n_workflow_id == "n8n-1"
        assert binding.side_effects == "read_only"
        assert binding.approval_policy == "none"


@pytest.mark.integration
def test_reloading_an_unchanged_file_reuses_the_existing_snapshot(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        first, _ = reload_registry(session, path, server_max_argument_bytes=262_144)
    with session_scope(session_factory) as session:
        second, reused = reload_registry(session, path, server_max_argument_bytes=262_144)
        assert reused is True
        assert second.id == first.id


@pytest.mark.integration
def test_reloading_changed_content_creates_a_new_snapshot_not_a_mutation(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        first, _ = reload_registry(session, path, server_max_argument_bytes=262_144)
        first_document = dict(first.document)

    path.write_text(VALID_DOCUMENT.replace("risk: low", "risk: medium"))
    with session_scope(session_factory) as session:
        second, reused = reload_registry(session, path, server_max_argument_bytes=262_144)
        assert reused is False
        assert second.id != first.id
        assert second.content_hash != first.content_hash

    # The original snapshot row is untouched — immutability, not mutation.
    with session_scope(session_factory) as session:
        from n8n_operator.storage.repository import RegistrySnapshotRepository

        reloaded_first = RegistrySnapshotRepository(session).get(first.id)
        assert reloaded_first is not None
        assert dict(reloaded_first.document) == first_document


@pytest.mark.integration
def test_a_failed_reload_never_touches_storage(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    good_path = _write(tmp_path, VALID_DOCUMENT, "good.yaml")
    with session_scope(session_factory) as session:
        good_snapshot, _ = reload_registry(session, good_path, server_max_argument_bytes=262_144)

    bad_path = _write(tmp_path, INVALID_DOCUMENT, "bad.yaml")
    with pytest.raises(RegistryValidationError), session_scope(session_factory) as session:
        reload_registry(session, bad_path, server_max_argument_bytes=262_144)

    with session_scope(session_factory) as session:
        active = get_active_snapshot(session)
        assert active is not None
        assert active.id == good_snapshot.id  # unchanged by the failed attempt


@pytest.mark.integration
def test_disabled_entries_get_no_workflow_binding(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    document = VALID_DOCUMENT + "    enabled: false\n"
    path = _write(tmp_path, document)
    with session_scope(session_factory) as session:
        snapshot, _ = reload_registry(session, path, server_max_argument_bytes=262_144)

    with session_scope(session_factory) as session:
        binding = WorkflowBindingRepository(session).get_by_snapshot_and_workflow_id(
            snapshot.id, "wf.a"
        )
        assert binding is None  # disabled -> no binding, indistinguishable from absent


@pytest.mark.integration
def test_audit_hook_is_called_when_provided(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    class _RecordingAuditHook:
        def record_registry_reload(
            self,
            session: Session,
            *,
            previous_snapshot_id: str | None,
            new_snapshot_id: str,
            content_hash: str,
            reused_existing: bool,
        ) -> None:
            calls.append(
                {
                    "previous_snapshot_id": previous_snapshot_id,
                    "new_snapshot_id": new_snapshot_id,
                    "content_hash": content_hash,
                    "reused_existing": reused_existing,
                }
            )

    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        snapshot, _ = reload_registry(
            session, path, server_max_argument_bytes=262_144, audit_hook=_RecordingAuditHook()
        )

    assert len(calls) == 1
    assert calls[0]["previous_snapshot_id"] is None
    assert calls[0]["new_snapshot_id"] == snapshot.id
    assert calls[0]["reused_existing"] is False


@pytest.mark.integration
def test_audit_hook_is_not_called_when_none_is_provided(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The phase-2 default: no audit support exists yet, so nothing is called — this is
    the "otherwise define the hook used in phase 3" behavior."""
    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        reload_registry(
            session, path, server_max_argument_bytes=262_144
        )  # audit_hook=None, no raise


@pytest.mark.integration
def test_reload_never_affects_a_reference_to_the_old_snapshot(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Simulates what a future prepared operation relies on: a snapshot_id captured
    before a reload must still resolve to the exact same document afterwards."""
    path = _write(tmp_path, VALID_DOCUMENT)
    with session_scope(session_factory) as session:
        old_snapshot, _ = reload_registry(session, path, server_max_argument_bytes=262_144)
        captured_snapshot_id = old_snapshot.id
        captured_document = dict(old_snapshot.document)

    path.write_text(
        VALID_DOCUMENT.replace("risk: low", "risk: high").replace(
            "approval: none", "approval: required"
        )
    )
    with session_scope(session_factory) as session:
        reload_registry(session, path, server_max_argument_bytes=262_144)

    with session_scope(session_factory) as session:
        from n8n_operator.storage.repository import RegistrySnapshotRepository

        still_there = RegistrySnapshotRepository(session).get(captured_snapshot_id)
        assert still_there is not None
        assert dict(still_there.document) == captured_document
