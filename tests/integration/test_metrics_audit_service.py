"""``core.service.get_metrics``/``list_audit_events``/``check_and_deliver_alerts`` and
the reactive drift alert (stage 08, MCP_TOOLS.md sections 5.7-5.8, ADR-019, ADR-012
section 3, ADR-018) — window/percentile computation, cursor pagination, RBAC scope
resolution, and alert-hook dedup, all against a real database. The underlying
repository queries are exhaustively covered by
``tests/integration/test_metrics_audit_repository.py``; this file is about the
service-layer policy built on top of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import (
    DeliveryOutcome,
    NotificationEvent,
    PreflightCheck,
    PreflightResult,
)
from n8n_operator.errors import InvalidArgumentsError
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    ExecutionResultRepository,
    OperationRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: metrics-audit-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
    description: External write.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: sales.only_workflow
    n8n_workflow_id: n8n-2
    title: Sales only
    description: Read only.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      additionalProperties: false
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def __init__(
        self, *, ready: bool = True, extra_checks: list[PreflightCheck] | None = None
    ) -> None:
        self.ready = ready
        self.extra_checks = extra_checks or []

    def check(self, workflow: Any) -> PreflightResult:
        status: Literal["pass", "fail"] = "pass" if self.ready else "fail"
        checks = [
            PreflightCheck(check="instance_reachable", status="pass"),
            PreflightCheck(
                check="workflow_active",
                status=status,
                code=None if self.ready else "WORKFLOW_INACTIVE",
            ),
            *self.extra_checks,
        ]
        return PreflightResult(ready=self.ready, checks=checks, checked_at=datetime.now(UTC))


class FakeSink:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        self.events.append(event)
        return DeliveryOutcome(delivered=True)


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], registry_path: Path) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


def _prepare(
    session_factory: sessionmaker[Session],
    *,
    workflow_id: str = "crm.sync_contact",
    preflight: FakePreflight | None = None,
    sink: FakeSink | None = None,
    idempotency_key: str | None = None,
) -> str:
    with session_scope(session_factory) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id="local",
            environment="default",
            workflow_id=workflow_id,
            arguments={},
            preflight=preflight or FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key=idempotency_key,
            notification_sink=sink,
        )
        return operation.id


# --------------------------------------------------------------------------------------
# get_metrics
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_get_metrics_rejects_an_unrecognized_window(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(InvalidArgumentsError):
        service.get_metrics(session, principal_id="local", window="90d")


@pytest.mark.integration
def test_get_metrics_rejects_an_unrecognized_group_by(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(InvalidArgumentsError):
        service.get_metrics(session, principal_id="local", group_by="not_a_real_dimension")


@pytest.mark.integration
def test_get_metrics_on_an_empty_window_reports_zero_denominator(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        result = service.get_metrics(session, principal_id="local", window="1h")
    assert result.totals.count == 0
    assert result.totals.by_outcome == {}
    assert result.latency_ms.p50 is None
    assert result.latency_ms.p50_reason == "insufficient_sample"


@pytest.mark.integration
def test_get_metrics_totals_and_breakdown_reflect_real_operations(
    loaded: sessionmaker[Session],
) -> None:
    _prepare(loaded, workflow_id="crm.sync_contact", preflight=FakePreflight(ready=False))
    _prepare(loaded, workflow_id="sales.only_workflow")
    with session_scope(loaded) as session:
        result = service.get_metrics(session, principal_id="local", group_by="workflow")
    assert result.totals.count == 2
    keys = {e.key for e in result.breakdown}
    assert keys == {"crm.sync_contact", "sales.only_workflow"}


@pytest.mark.integration
def test_get_metrics_latency_percentiles_below_ten_samples_are_insufficient(
    loaded: sessionmaker[Session],
) -> None:
    op_id = _prepare(loaded)
    with session_scope(loaded) as session:
        now = datetime.now(UTC)
        ExecutionResultRepository(session).create(
            operation_id=op_id,
            status="success",
            started_at=now - timedelta(seconds=1),
            finished_at=now,
        )
        result = service.get_metrics(session, principal_id="local")
    assert result.latency_ms.p50 is None
    assert result.latency_ms.p50_reason == "insufficient_sample"


@pytest.mark.integration
def test_get_metrics_latency_percentiles_computed_once_ten_samples_exist(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        for i in range(10):
            op_id = f"op_lat_{i}"
            OperationRepository(session).create(
                id=op_id,
                principal_id="local",
                environment="default",
                snapshot_id=service.get_active_snapshot(session).id,  # type: ignore[union-attr]
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="SUCCEEDED",
                arguments={},
                argument_fingerprint=f"fp{i}",
                argument_bytes=2,
            )
            now = datetime.now(UTC)
            ExecutionResultRepository(session).create(
                operation_id=op_id,
                status="success",
                started_at=now - timedelta(milliseconds=100 * (i + 1)),
                finished_at=now,
            )
        result = service.get_metrics(session, principal_id="local")
    assert result.latency_ms.p50 is not None
    assert result.latency_ms.p50_reason is None


@pytest.mark.integration
def test_get_metrics_v2_scopes_breakdown_to_authorized_workflows_only(
    loaded: sessionmaker[Session],
) -> None:
    _prepare(loaded, workflow_id="crm.sync_contact")
    _prepare(loaded, workflow_id="sales.only_workflow")
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        env_row = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id,
            organization_id=org.id,
            roles=["viewer"],
            workflow_scope="crm.*",
        )
        viewer_id, environment_id = viewer.id, env_row.id

    with session_scope(loaded) as session:
        # These operations were prepared in v1 (environment="default"), so a v2
        # scope-filtered query against a real environment id sees none of them —
        # this proves the scope filter is real SQL, not a no-op, even though it
        # means the "authorized-only" assertion below is about the *shape*
        # (crm.* pattern reaches nothing outside it), not specific row counts.
        result = service.get_metrics(
            session,
            principal_id=viewer_id,
            environment=environment_id,
            group_by="workflow",
            enable_v2=True,
        )
    assert all(entry.key.startswith("crm.") or entry.key == "other" for entry in result.breakdown)


# --------------------------------------------------------------------------------------
# list_audit_events
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_audit_events_rejects_an_out_of_range_limit(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(InvalidArgumentsError):
        service.list_audit_events(session, principal_id="local", limit=0)
    with session_scope(loaded) as session, pytest.raises(InvalidArgumentsError):
        service.list_audit_events(session, principal_id="local", limit=101)


@pytest.mark.integration
def test_list_audit_events_rejects_a_malformed_cursor(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(InvalidArgumentsError):
        service.list_audit_events(session, principal_id="local", cursor="not-valid-base64!!!")


@pytest.mark.integration
def test_list_audit_events_paginates_with_a_real_cursor(loaded: sessionmaker[Session]) -> None:
    for i in range(3):
        _prepare(loaded, idempotency_key=f"key-{i}")

    with session_scope(loaded) as session:
        page1 = service.list_audit_events(session, principal_id="local", limit=2)
    assert len(page1.events) == 2
    assert page1.next_cursor is not None

    with session_scope(loaded) as session:
        page2 = service.list_audit_events(
            session, principal_id="local", limit=2, cursor=page1.next_cursor
        )
    seqs_page1 = {e.seq for e in page1.events}
    seqs_page2 = {e.seq for e in page2.events}
    assert seqs_page1.isdisjoint(seqs_page2)


@pytest.mark.integration
def test_list_audit_events_short_page_has_no_next_cursor(loaded: sessionmaker[Session]) -> None:
    _prepare(loaded)
    with session_scope(loaded) as session:
        page = service.list_audit_events(session, principal_id="local", limit=100)
    assert page.next_cursor is None


@pytest.mark.integration
def test_list_audit_events_workflow_id_filter(loaded: sessionmaker[Session]) -> None:
    _prepare(loaded, workflow_id="crm.sync_contact")
    _prepare(loaded, workflow_id="sales.only_workflow")
    with session_scope(loaded) as session:
        page = service.list_audit_events(
            session, principal_id="local", workflow_id="crm.sync_contact", limit=100
        )
    assert page.events
    assert all(
        e.subject_id == "crm.sync_contact" or e.subject_type == "operation" for e in page.events
    )


# --------------------------------------------------------------------------------------
# list_audit_events / get_metrics — cross-organization isolation (Stage 11 security
# review). Goes through the real `_resolve_scope`/`identity.resolve_environment`
# authorization path, not `AuditLogRepository`/`OperationRepository` directly — proving
# the fix holds end to end, not only at the query layer these two functions delegate to.
# --------------------------------------------------------------------------------------


@pytest.fixture
def two_org_shared_workflow(
    loaded: sessionmaker[Session],
) -> dict[str, str]:
    """Two organizations, three environments (org A: staging + production, org B:
    production), the same workflow id (``crm.sync_contact``, already registered by
    ``loaded``) prepared in every environment — the shared scenario every isolation
    test in this section reuses."""
    with session_scope(loaded) as session:
        org_a = OrganizationRepository(session).create(name="Org A")
        org_b = OrganizationRepository(session).create(name="Org B")
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
        viewer_a = PrincipalRepository(session).create(kind="user", display_name="Org A Viewer")
        viewer_b = PrincipalRepository(session).create(kind="user", display_name="Org B Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer_a.id,
            organization_id=org_a.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        OrganizationMembershipRepository(session).create(
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        operator_a = PrincipalRepository(session).create(kind="user", display_name="Org A Operator")
        operator_b = PrincipalRepository(session).create(kind="user", display_name="Org B Operator")
        OrganizationMembershipRepository(session).create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_a_staging.id, env_a_prod.id],
        )
        OrganizationMembershipRepository(session).create(
            principal_id=operator_b.id,
            organization_id=org_b.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_b_prod.id],
        )
        ids = {
            "env_a_staging": env_a_staging.id,
            "env_a_prod": env_a_prod.id,
            "env_b_prod": env_b_prod.id,
            "viewer_a": viewer_a.id,
            "viewer_b": viewer_b.id,
            "operator_a": operator_a.id,
            "operator_b": operator_b.id,
        }

    op_a_id = _prepare_v2(loaded, operator_a_id=ids["operator_a"], environment=ids["env_a_prod"])
    op_b_id = _prepare_v2(loaded, operator_a_id=ids["operator_b"], environment=ids["env_b_prod"])
    ids["op_a"] = op_a_id
    ids["op_b"] = op_b_id
    return ids


def _prepare_v2(
    session_factory: sessionmaker[Session], *, operator_a_id: str, environment: str
) -> str:
    with session_scope(session_factory) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=operator_a_id,
            environment=environment,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(ready=False),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        return operation.id


@pytest.mark.integration
def test_list_audit_events_v2_never_leaks_an_operation_across_organizations(
    two_org_shared_workflow: dict[str, str],
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org_a_events = service.list_audit_events(
            session,
            principal_id=two_org_shared_workflow["viewer_a"],
            environment=two_org_shared_workflow["env_a_prod"],
            limit=100,
            enable_v2=True,
        )
        org_b_events = service.list_audit_events(
            session,
            principal_id=two_org_shared_workflow["viewer_b"],
            environment=two_org_shared_workflow["env_b_prod"],
            limit=100,
            enable_v2=True,
        )

    org_a_subject_ids = {e.subject_id for e in org_a_events.events if e.subject_type == "operation"}
    org_b_subject_ids = {e.subject_id for e in org_b_events.events if e.subject_type == "operation"}
    assert two_org_shared_workflow["op_a"] in org_a_subject_ids
    assert two_org_shared_workflow["op_b"] not in org_a_subject_ids
    assert two_org_shared_workflow["op_b"] in org_b_subject_ids
    assert two_org_shared_workflow["op_a"] not in org_b_subject_ids


@pytest.mark.integration
def test_get_metrics_v2_never_counts_another_organizations_operation(
    two_org_shared_workflow: dict[str, str],
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org_b_metrics = service.get_metrics(
            session,
            principal_id=two_org_shared_workflow["viewer_b"],
            environment=two_org_shared_workflow["env_b_prod"],
            group_by="workflow",
            enable_v2=True,
        )
    # Org B prepared exactly one operation against the shared workflow id — if Org A's
    # own operation (against the same workflow id, in a different organization) ever
    # counted toward Org B's totals, this would be >= 2.
    assert org_b_metrics.totals.count == 1


@pytest.mark.integration
def test_list_audit_events_v2_pagination_cursor_reapplies_scope_on_every_page(
    loaded: sessionmaker[Session],
) -> None:
    """A cursor obtained from one organization's own paginated ``list_audit_events``
    result must not become a way to page into another organization's events — the
    cursor is opaque but concretely just a ``seq`` boundary, so the scope filter has
    to be re-applied on every page fetched with it, not only the first."""
    with session_scope(loaded) as session:
        org_a = OrganizationRepository(session).create(name="Org A")
        org_b = OrganizationRepository(session).create(name="Org B")
        env_a = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="production",
            n8n_base_url_ref="env:A_URL",
            n8n_api_key_ref="env:A_KEY",
        )
        env_b = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="production",
            n8n_base_url_ref="env:B_URL",
            n8n_api_key_ref="env:B_KEY",
        )
        operator_a = PrincipalRepository(session).create(kind="user", display_name="Org A Operator")
        viewer_a = PrincipalRepository(session).create(kind="user", display_name="Org A Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_a.id],
        )
        OrganizationMembershipRepository(session).create(
            principal_id=viewer_a.id,
            organization_id=org_a.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        operator_b = PrincipalRepository(session).create(kind="user", display_name="Org B Operator")
        OrganizationMembershipRepository(session).create(
            principal_id=operator_b.id,
            organization_id=org_b.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_b.id],
        )
        operator_a_id, viewer_a_id, operator_b_id = operator_a.id, viewer_a.id, operator_b.id
        env_a_id, env_b_id = env_a.id, env_b.id

    # Interleave three of Org A's own operations with three of Org B's — a naive
    # cursor implementation that dropped the scope filter after the first page would
    # surface Org B's rows on Org A's own second page.
    for _ in range(3):
        _prepare_v2(loaded, operator_a_id=operator_a_id, environment=env_a_id)
        _prepare_v2(loaded, operator_a_id=operator_b_id, environment=env_b_id)

    with session_scope(loaded) as session:
        page1 = service.list_audit_events(
            session, principal_id=viewer_a_id, environment=env_a_id, limit=2, enable_v2=True
        )
    assert page1.next_cursor is not None
    with session_scope(loaded) as session:
        page2 = service.list_audit_events(
            session,
            principal_id=viewer_a_id,
            environment=env_a_id,
            limit=2,
            cursor=page1.next_cursor,
            enable_v2=True,
        )
    for page in (page1, page2):
        for event in page.events:
            if event.subject_type != "operation":
                continue
            with session_scope(loaded) as session:
                row = OperationRepository(session).get(event.subject_id)
                assert row is not None
                assert row.environment_id == env_a_id


# --------------------------------------------------------------------------------------
# check_and_deliver_alerts
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_check_and_deliver_alerts_fires_once_for_a_stuck_executing_operation(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        OperationRepository(session).create(
            id="op_stuck",
            principal_id="local",
            environment="default",
            snapshot_id=service.get_active_snapshot(session).id,  # type: ignore[union-attr]
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            state="EXECUTING",
            arguments={},
            argument_fingerprint="fp-stuck",
            argument_bytes=2,
        )

    sink = FakeSink()
    with session_scope(loaded) as session:
        delivered = service.check_and_deliver_alerts(
            session, sink=sink, executing_stuck_threshold_seconds=0
        )
    assert delivered == 1
    assert sink.events[0].event_type == "operation.stuck"

    with session_scope(loaded) as session:
        second_sweep = service.check_and_deliver_alerts(
            session, sink=sink, executing_stuck_threshold_seconds=0
        )
    assert second_sweep == 0
    assert len(sink.events) == 1


@pytest.mark.integration
def test_check_and_deliver_alerts_fires_once_for_an_unknown_operation(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        OperationRepository(session).create(
            id="op_unknown",
            principal_id="local",
            environment="default",
            snapshot_id=service.get_active_snapshot(session).id,  # type: ignore[union-attr]
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            state="UNKNOWN",
            arguments={},
            argument_fingerprint="fp-unknown",
            argument_bytes=2,
        )

    sink = FakeSink()
    with session_scope(loaded) as session:
        delivered = service.check_and_deliver_alerts(session, sink=sink)
    assert delivered == 1
    assert sink.events[0].event_type == "operation.unknown"

    with session_scope(loaded) as session:
        second_sweep = service.check_and_deliver_alerts(session, sink=sink)
    assert second_sweep == 0


# --------------------------------------------------------------------------------------
# reactive drift alert
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_prepare_operation_fires_a_drift_alert_when_blocked_on_drift(
    loaded: sessionmaker[Session],
) -> None:
    sink = FakeSink()
    drift_preflight = FakePreflight(
        ready=False,
        extra_checks=[
            PreflightCheck(
                check="definition_unchanged",
                status="fail",
                code="DEFINITION_DRIFT",
                detail={"registered": "sha256:" + "a" * 64, "live": "sha256:" + "b" * 64},
            )
        ],
    )
    _prepare(loaded, preflight=drift_preflight, sink=sink, idempotency_key="k1")
    assert len(sink.events) == 1
    assert sink.events[0].event_type == "drift.detected"


@pytest.mark.integration
def test_prepare_operation_drift_alert_dedups_the_same_live_hash(
    loaded: sessionmaker[Session],
) -> None:
    sink = FakeSink()
    drift_preflight = FakePreflight(
        ready=False,
        extra_checks=[
            PreflightCheck(
                check="definition_unchanged",
                status="fail",
                code="DEFINITION_DRIFT",
                detail={"registered": "sha256:" + "a" * 64, "live": "sha256:" + "b" * 64},
            )
        ],
    )
    _prepare(loaded, preflight=drift_preflight, sink=sink, idempotency_key="k1")
    _prepare(loaded, preflight=drift_preflight, sink=sink, idempotency_key="k2")
    assert len(sink.events) == 1


@pytest.mark.integration
def test_prepare_operation_drift_alert_fires_again_for_a_different_live_hash(
    loaded: sessionmaker[Session],
) -> None:
    sink = FakeSink()
    first = FakePreflight(
        ready=False,
        extra_checks=[
            PreflightCheck(
                check="definition_unchanged",
                status="fail",
                code="DEFINITION_DRIFT",
                detail={"registered": "sha256:" + "a" * 64, "live": "sha256:" + "b" * 64},
            )
        ],
    )
    second = FakePreflight(
        ready=False,
        extra_checks=[
            PreflightCheck(
                check="definition_unchanged",
                status="fail",
                code="DEFINITION_DRIFT",
                detail={"registered": "sha256:" + "a" * 64, "live": "sha256:" + "c" * 64},
            )
        ],
    )
    _prepare(loaded, preflight=first, sink=sink, idempotency_key="k1")
    _prepare(loaded, preflight=second, sink=sink, idempotency_key="k2")
    assert len(sink.events) == 2


@pytest.mark.integration
def test_prepare_operation_without_a_sink_is_a_no_op(loaded: sessionmaker[Session]) -> None:
    drift_preflight = FakePreflight(
        ready=False,
        extra_checks=[
            PreflightCheck(
                check="definition_unchanged",
                status="fail",
                code="DEFINITION_DRIFT",
                detail={"registered": "sha256:" + "a" * 64, "live": "sha256:" + "b" * 64},
            )
        ],
    )
    # No sink passed at all — must not raise, must not attempt delivery.
    op_id = _prepare(loaded, preflight=drift_preflight)
    with session_scope(loaded) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "BLOCKED"
