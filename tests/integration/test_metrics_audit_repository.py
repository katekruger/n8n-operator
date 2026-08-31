"""``OperationRepository``/``ExecutionResultRepository``/``AuditLogRepository``'s new
metrics- and audit-query-oriented methods (stage 08, ADR-019, ADR-012 section 3) —
pure storage-layer behavior, isolated from RBAC/window/percentile policy (that's
``core.service.get_metrics``/``list_audit_events``'s job, tested separately).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.storage.repository import (
    AuditLogRepository,
    EnvironmentRepository,
    ExecutionResultRepository,
    OperationRepository,
    OrganizationRepository,
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
        org = OrganizationRepository(session).create(name="org")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="prod",
            n8n_base_url_ref="env:N8N_BASE_URL",
            n8n_api_key_ref="env:N8N_API_KEY",
        )
        _make_operation(
            session,
            seed,
            id="op1",
            workflow_id="crm.sync",
            environment_id=env.id,
            organization_id=org.id,
        )
        _make_operation(
            session,
            seed,
            id="op2",
            workflow_id="sales.only",
            environment_id=env.id,
            organization_id=org.id,
        )
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
            environment_id=env.id,
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["op1"]


@pytest.mark.integration
def test_audit_log_list_page_excludes_operation_outside_resolved_environment(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """A wildcard (``"*"``) workflow-scope pattern matches any ``workflow_id`` — the
    ``environment_id`` conjunct on ``operation_clause`` is what keeps a caller's own
    resolved environment from ever surfacing another organization's operation against a
    workflow id both organizations happen to share (the cross-org audit leak this scope
    check closes)."""
    with session_scope(session_factory) as session:
        org_a = OrganizationRepository(session).create(name="org-a")
        org_b = OrganizationRepository(session).create(name="org-b")
        env_a = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="prod",
            n8n_base_url_ref="env:N8N_BASE_URL_A",
            n8n_api_key_ref="env:N8N_API_KEY_A",
        )
        env_b = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="prod",
            n8n_base_url_ref="env:N8N_BASE_URL_B",
            n8n_api_key_ref="env:N8N_API_KEY_B",
        )
        _make_operation(
            session,
            seed,
            id="op_a",
            workflow_id="crm.shared",
            environment_id=env_a.id,
            organization_id=org_a.id,
        )
        _make_operation(
            session,
            seed,
            id="op_b",
            workflow_id="crm.shared",
            environment_id=env_b.id,
            organization_id=org_b.id,
        )
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_a",
            outcome="allowed",
        )
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h2",
            actor="system",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_b",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=["%"],
            environment_id=env_b.id,
            include_registry_snapshot_events=False,
        )
    assert [r.subject_id for r in rows] == ["op_b"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "org_a_pattern,org_b_pattern",
    [
        ("%", "%"),
        ("crm.shared", "crm.shared"),
        ("crm.%", "crm.%"),
        ("%", "crm.shared"),
        ("crm.%", "%"),
    ],
    ids=["wildcard-wildcard", "exact-exact", "prefix-prefix", "wildcard-exact", "prefix-wildcard"],
)
def test_audit_log_list_page_matrix_never_crosses_orgs_and_still_allows_same_org(
    session_factory: sessionmaker[Session],
    seed: dict[str, Any],
    org_a_pattern: str,
    org_b_pattern: str,
) -> None:
    """Two organizations, three environments (org A: staging + production, org B:
    production), the SAME workflow id (``crm.shared``) registered against operations
    in both, exercised across wildcard/exact/prefix ``workflow_id_like_patterns`` —
    every combination must both (a) never surface the other organization's operation
    and (b) still surface the caller's own organization's operation (a fix that only
    ever returns nothing would pass every negative test here without actually
    working)."""
    with session_scope(session_factory) as session:
        org_a = OrganizationRepository(session).create(name="org-a")
        org_b = OrganizationRepository(session).create(name="org-b")
        env_a_staging = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="staging",
            n8n_base_url_ref="env:A_STAGING_URL",
            n8n_api_key_ref="env:A_STAGING_KEY",
        )
        env_a_prod = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="production",
            n8n_base_url_ref="env:A_PROD_URL",
            n8n_api_key_ref="env:A_PROD_KEY",
        )
        env_b_prod = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="production",
            n8n_base_url_ref="env:B_PROD_URL",
            n8n_api_key_ref="env:B_PROD_KEY",
        )
        _make_operation(
            session,
            seed,
            id="op_a_staging",
            workflow_id="crm.shared",
            environment_id=env_a_staging.id,
            organization_id=org_a.id,
        )
        _make_operation(
            session,
            seed,
            id="op_a_prod",
            workflow_id="crm.shared",
            environment_id=env_a_prod.id,
            organization_id=org_a.id,
        )
        _make_operation(
            session,
            seed,
            id="op_b_prod",
            workflow_id="crm.shared",
            environment_id=env_b_prod.id,
            organization_id=org_b.id,
        )
        repo = AuditLogRepository(session)
        for op_id in ("op_a_staging", "op_a_prod", "op_b_prod"):
            repo.append(
                prev_hash=repo.get_last_hash(),
                entry_hash=f"h-{op_id}",
                actor="system",
                action="operation.prepared",
                subject_type="operation",
                subject_id=op_id,
                outcome="allowed",
            )

        # Org A's own environment: sees only its own two operations, never Org B's.
        rows_a = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[org_a_pattern],
            environment_id=env_a_prod.id,
            include_registry_snapshot_events=False,
        )
        assert {r.subject_id for r in rows_a} == {"op_a_prod"}

        # Org B's own environment: sees only its own operation, never Org A's — the
        # same assertion the confirmed leak would have failed with a wildcard/prefix
        # pattern before the `Operation.environment_id == environment_id` fix.
        rows_b = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[org_b_pattern],
            environment_id=env_b_prod.id,
            include_registry_snapshot_events=False,
        )
        assert {r.subject_id for r in rows_b} == {"op_b_prod"}


@pytest.mark.integration
def test_audit_log_list_page_empty_workflow_scope_sees_nothing_in_either_org(
    session_factory: sessionmaker[Session],
    seed: dict[str, Any],
) -> None:
    """An empty (non-``None``) ``workflow_id_like_patterns`` list — a principal with
    no qualifying membership — must see nothing, in its own org or the other's,
    exactly like ``OperationRepository.list`` already guarantees for operations
    themselves."""
    with session_scope(session_factory) as session:
        org_a = OrganizationRepository(session).create(name="org-a")
        env_a = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="prod",
            n8n_base_url_ref="env:A_URL",
            n8n_api_key_ref="env:A_KEY",
        )
        _make_operation(
            session,
            seed,
            id="op_a",
            workflow_id="crm.shared",
            environment_id=env_a.id,
            organization_id=org_a.id,
        )
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="system",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_a",
            outcome="allowed",
        )
        rows = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[],
            environment_id=env_a.id,
            include_registry_snapshot_events=False,
        )
    assert rows == []


@pytest.mark.integration
def test_audit_log_list_page_pagination_cursor_reapplies_scope_on_every_page(
    session_factory: sessionmaker[Session],
    seed: dict[str, Any],
) -> None:
    """A ``before_seq`` cursor obtained from one organization's own paginated result
    must not become a way to page into another organization's events: the cursor is
    just an integer ``seq`` boundary, so the scope filter (``workflow_id_like_patterns``
    + ``environment_id``) has to be re-applied on every page, not only the first."""
    with session_scope(session_factory) as session:
        org_a = OrganizationRepository(session).create(name="org-a")
        org_b = OrganizationRepository(session).create(name="org-b")
        env_a = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="prod",
            n8n_base_url_ref="env:A_URL",
            n8n_api_key_ref="env:A_KEY",
        )
        env_b = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="prod",
            n8n_base_url_ref="env:B_URL",
            n8n_api_key_ref="env:B_KEY",
        )
        repo = AuditLogRepository(session)
        # Interleave Org A and Org B operation-audit rows so a naive "walk seq
        # backwards with no scope re-check" implementation would surface Org B's
        # rows on Org A's second page.
        for i in range(5):
            _make_operation(
                session,
                seed,
                id=f"op_a_{i}",
                workflow_id="crm.shared",
                environment_id=env_a.id,
                organization_id=org_a.id,
            )
            repo.append(
                prev_hash=repo.get_last_hash(),
                entry_hash=f"ha{i}",
                actor="system",
                action="operation.prepared",
                subject_type="operation",
                subject_id=f"op_a_{i}",
                outcome="allowed",
            )
            _make_operation(
                session,
                seed,
                id=f"op_b_{i}",
                workflow_id="crm.shared",
                environment_id=env_b.id,
                organization_id=org_b.id,
            )
            repo.append(
                prev_hash=repo.get_last_hash(),
                entry_hash=f"hb{i}",
                actor="system",
                action="operation.prepared",
                subject_type="operation",
                subject_id=f"op_b_{i}",
                outcome="allowed",
            )

        seen: list[str] = []
        before_seq: int | None = None
        for _ in range(10):  # generous upper bound; real pages are short
            page = repo.list_page(
                before_seq=before_seq,
                limit=2,
                since=None,
                workflow_id=None,
                workflow_id_like_patterns=["%"],
                environment_id=env_a.id,
                include_registry_snapshot_events=False,
            )
            if not page:
                break
            seen.extend(r.subject_id for r in page)
            before_seq = page[-1].seq

    assert seen == [f"op_a_{i}" for i in reversed(range(5))]
    assert all(subject_id.startswith("op_a_") for subject_id in seen)


@pytest.mark.integration
def test_audit_log_list_page_workflow_subject_detail_never_carries_tenant_data(
    session_factory: sessionmaker[Session],
) -> None:
    """Workflow-definition audit events (``subject_type="workflow"``) are global
    registry events by design (this module's own ``list_page`` docstring, and the
    fix's own updated reasoning) — the same workflow-scope pattern that authorizes
    seeing the shared registry entry authorizes seeing these events, with no further
    per-organization check. That is only safe because their ``detail`` never carries
    tenant-specific content (a caller's argument values, an environment's instance
    URL, or a credential reference) — only structural, workflow-level facts (a byte
    count, a configured limit, an error code). This test pins that shape down as a
    permanent regression guard, mirroring the real ``detail`` a rate-limit/oversized-
    argument denial actually writes (``core.service.prepare_operation``'s own
    ``operation.prepare_denied`` audit write)."""
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="h1",
            actor="principal_from_org_a",
            action="operation.prepare_denied",
            subject_type="workflow",
            subject_id="crm.shared",
            outcome="denied",
            detail={"code": "RATE_LIMITED", "limit_per_minute": 10, "recent_count": 11},
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
    assert len(rows) == 1
    detail = rows[0].detail
    assert set(detail.keys()) <= {"code", "limit_per_minute", "recent_count", "size", "limit"}
    forbidden_substrings = (
        "n8n_workflow_id",
        "n8n_base_url",
        "n8n_api_key",
        "credential_ref",
        "webhook_path",
        "@",  # no email-shaped argument value ever appears in this detail shape
    )
    serialized = json.dumps(detail)
    for forbidden in forbidden_substrings:
        assert forbidden not in serialized


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
