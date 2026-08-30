"""``OperationRepository``/``ExecutionResultRepository``/``AuditLogRepository``'s new
metrics- and audit-query-oriented methods (stage 08, ADR-019, ADR-012 section 3) —
pure storage-layer behavior, isolated from RBAC/window/percentile policy (that's
``core.service.get_metrics``/``list_audit_events``'s job, tested separately).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.storage.repository import (
    AuditLogRepository,
    ExecutionResultRepository,
    OperationRepository,
)
from n8n_operator.storage.session import session_scope

from .test_repository import _make_operation


@pytest.mark.integration
def test_count_by_outcome_groups_by_state(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="SUCCEEDED")
        _make_operation(session, seed, id="op2", state="SUCCEEDED")
        _make_operation(session, seed, id="op3", state="FAILED")
        counts = OperationRepository(session).count_by_outcome(
            workflow_id_like_patterns=None, environment=None, since=None
        )
    assert counts == {"SUCCEEDED": 2, "FAILED": 1}


@pytest.mark.integration
def test_count_by_outcome_empty_scope_list_matches_nothing(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="SUCCEEDED")
        counts = OperationRepository(session).count_by_outcome(
            workflow_id_like_patterns=[], environment=None, since=None
        )
    assert counts == {}


@pytest.mark.integration
def test_count_by_outcome_scopes_by_workflow_pattern(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", workflow_id="crm.sync", state="SUCCEEDED")
        _make_operation(session, seed, id="op2", workflow_id="sales.only", state="SUCCEEDED")
        counts = OperationRepository(session).count_by_outcome(
            workflow_id_like_patterns=["crm.%"], environment=None, since=None
        )
    assert counts == {"SUCCEEDED": 1}


@pytest.mark.integration
def test_count_by_outcome_respects_since(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="SUCCEEDED")
        counts_now = OperationRepository(session).count_by_outcome(
            workflow_id_like_patterns=None,
            environment=None,
            since=datetime.now(UTC) - timedelta(hours=1),
        )
        counts_future = OperationRepository(session).count_by_outcome(
            workflow_id_like_patterns=None,
            environment=None,
            since=datetime.now(UTC) + timedelta(hours=1),
        )
    assert counts_now == {"SUCCEEDED": 1}
    assert counts_future == {}


@pytest.mark.integration
def test_breakdown_by_workflow_sorted_most_frequent_first(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", workflow_id="wf.a", state="SUCCEEDED")
        _make_operation(session, seed, id="op2", workflow_id="wf.b", state="SUCCEEDED")
        _make_operation(session, seed, id="op3", workflow_id="wf.b", state="FAILED")
        rows = OperationRepository(session).breakdown_by_workflow(
            workflow_id_like_patterns=None, environment=None, since=None
        )
    assert rows[0] == ("wf.b", 2, {"SUCCEEDED": 1, "FAILED": 1})
    assert rows[1] == ("wf.a", 1, {"SUCCEEDED": 1})


@pytest.mark.integration
def test_stuck_executing_finds_only_old_executing_rows(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="EXECUTING")
        _make_operation(session, seed, id="op2", state="SUCCEEDED")
        stuck = OperationRepository(session).stuck_executing(
            older_than=datetime.now(UTC) + timedelta(hours=1)
        )
        not_stuck_yet = OperationRepository(session).stuck_executing(
            older_than=datetime.now(UTC) - timedelta(hours=1)
        )
    assert [op.id for op in stuck] == ["op1"]
    assert not_stuck_yet == []


@pytest.mark.integration
def test_list_unknown_finds_only_unknown_rows(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="UNKNOWN")
        _make_operation(session, seed, id="op2", state="SUCCEEDED")
        unknown = OperationRepository(session).list_unknown()
    assert [op.id for op in unknown] == ["op1"]


@pytest.mark.integration
def test_list_finished_durations_ms_computes_from_started_and_finished(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="SUCCEEDED")
        started = datetime.now(UTC) - timedelta(seconds=2)
        finished = datetime.now(UTC)
        ExecutionResultRepository(session).create(
            operation_id="op1", status="success", started_at=started, finished_at=finished
        )
        durations = ExecutionResultRepository(session).list_finished_durations_ms(
            workflow_id_like_patterns=None, environment=None, since=None
        )
    assert len(durations) == 1
    assert 1900 <= durations[0] <= 2100


@pytest.mark.integration
def test_list_finished_durations_ms_excludes_unfinished(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", state="EXECUTING")
        ExecutionResultRepository(session).create(operation_id="op1", status="indeterminate")
        durations = ExecutionResultRepository(session).list_finished_durations_ms(
            workflow_id_like_patterns=None, environment=None, since=None
        )
    assert durations == []


@pytest.mark.integration
def test_list_finished_durations_ms_respects_workflow_scope(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", workflow_id="crm.sync", state="SUCCEEDED")
        _make_operation(session, seed, id="op2", workflow_id="sales.only", state="SUCCEEDED")
        now = datetime.now(UTC)
        ExecutionResultRepository(session).create(
            operation_id="op1", status="success", started_at=now, finished_at=now
        )
        ExecutionResultRepository(session).create(
            operation_id="op2", status="success", started_at=now, finished_at=now
        )
        scoped = ExecutionResultRepository(session).list_finished_durations_ms(
            workflow_id_like_patterns=["crm.%"], environment=None, since=None
        )
    assert len(scoped) == 1


@pytest.mark.integration
def test_audit_log_list_page_orders_newest_first_and_paginates(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        prev = repo.get_last_hash()
        for i in range(5):
            entry = repo.append(
                prev_hash=prev,
                entry_hash=f"hash{i}",
                actor="system",
                action="operation.created",
                subject_type="workflow",
                subject_id="wf.a",
                outcome="allowed",
            )
            prev = entry.entry_hash

        page1 = repo.list_page(
            before_seq=None,
            limit=2,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=None,
            environment_id=None,
            include_registry_snapshot_events=True,
        )
        assert [e.entry_hash for e in page1] == ["hash4", "hash3"]

        page2 = repo.list_page(
            before_seq=page1[-1].seq,
            limit=2,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=None,
            environment_id=None,
            include_registry_snapshot_events=True,
        )
        assert [e.entry_hash for e in page2] == ["hash2", "hash1"]


@pytest.mark.integration
def test_audit_log_list_page_scopes_workflow_subject_type(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="crm.sync",
            outcome="allowed",
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h2",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="sales.only",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["crm.%"],
            environment_id=None,
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["crm.sync"]


@pytest.mark.integration
def test_audit_log_list_page_scopes_operation_subject_type_via_join(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op1", workflow_id="crm.sync")
        _make_operation(session, seed, id="op2", workflow_id="sales.only")
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op1",
            outcome="allowed",
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h2",
            actor="system",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op2",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["crm.%"],
            environment_id=None,
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["op1"]


@pytest.mark.integration
def test_audit_log_list_page_scopes_environment_subject_type_exactly(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="a",
            subject_type="environment",
            subject_id="env-a",
            outcome="allowed",
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h2",
            actor="system",
            action="a",
            subject_type="environment",
            subject_id="env-b",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["irrelevant-pattern"],
            environment_id="env-a",
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["env-a"]


@pytest.mark.integration
def test_audit_log_list_page_excludes_registry_snapshot_events_unless_included(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="registry.reloaded",
            subject_type="registry_snapshot",
            subject_id="snap1",
            outcome="allowed",
        )
        excluded = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["irrelevant"],
            environment_id=None,
            include_registry_snapshot_events=False,
        )
        included = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["irrelevant"],
            environment_id=None,
            include_registry_snapshot_events=True,
        )
    assert excluded == []
    assert [r.subject_id for r in included] == ["snap1"]


@pytest.mark.integration
def test_audit_log_list_page_empty_pattern_list_matches_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="crm.sync",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[],
            environment_id=None,
            include_registry_snapshot_events=False,
        )
    assert rows == []


@pytest.mark.integration
def test_audit_log_list_page_workflow_id_filter_restricts_further(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="crm.sync",
            outcome="allowed",
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h2",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="crm.other",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id="crm.sync",
            workflow_id_like_patterns=None,
            environment_id=None,
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["crm.sync"]
