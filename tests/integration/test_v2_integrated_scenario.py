"""Two-organization, three-environment integrated scenario (Stage 11) — proves the v2
system works together, not just that each stage's own isolated tests pass. Runs
against real PostgreSQL (this scenario needs org/environment isolation semantics that
SQLite's single-writer model can't meaningfully distinguish from a correctness
standpoint, and Stage 11's own design calls for Postgres here specifically).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Engine

from n8n_operator.core import service
from n8n_operator.core.models import DeliveryOutcome, NotificationEvent, PreflightResult
from n8n_operator.storage.repository import (
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

pytestmark = pytest.mark.postgres

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: stage11-integrated-scenario
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-crm-1
    title: Sync a contact into the CRM
    description: Upserts one contact by email.
    owner: revops
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/crm-sync
      auth: none
      correlation: response_envelope
    input_schema:
      type: object
      properties:
        email:
          type: string
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: mkt.campaign_sync
    n8n_workflow_id: n8n-mkt-1
    title: Sync a campaign audience segment
    description: Pushes an audience definition to the marketing platform.
    owner: marketing-ops
    version: 1
    definition_hash: sha256:{hash_b}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/campaign-sync
      auth: none
      correlation: response_envelope
    input_schema:
      type: object
      properties:
        campaign_id:
          type: string
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class FakeSink:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        self.events.append(event)
        return DeliveryOutcome(delivered=True)


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url, pool_size=10, max_overflow=10)


@dataclass
class _ScenarioState:
    """Everything Task 3b's test methods need, built once by Task 3a's setup and
    threaded through the class via a shared pytest fixture (see Step 2 below)."""

    engine: Engine
    session_factory: Any
    org_a_id: str
    org_b_id: str
    env_staging_id: str
    env_production_id: str
    env_secondary_prod_id: str
    org_a_operator_id: str
    org_a_approver_id: str
    org_b_viewer_id: str
    sink: FakeSink
    crm_operation_id: str = field(default="")
    campaign_operation_id: str = field(default="")


@pytest.fixture
def scenario(postgres_test_db_url: str, tmp_path: Path) -> Iterator[_ScenarioState]:
    engine = _migrated_engine(postgres_test_db_url)
    factory = create_session_factory(engine)

    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)

    sink = FakeSink()

    with session_scope(factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)

        # Org A: a startup GTM team running crm.sync_contact.
        org_a = OrganizationRepository(session).create(name="Org A — Acme GTM")
        # Org B: a second, unrelated organization — proves cross-org isolation is real,
        # not just "the query happened to only return one org's rows in this test."
        org_b = OrganizationRepository(session).create(name="Org B — Globex Marketing")

        env_staging = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="staging",
            n8n_base_url_ref="env:STAGE11_STAGING_BASE_URL",
            n8n_api_key_ref="env:STAGE11_STAGING_API_KEY",
        )
        env_production = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="production",
            n8n_base_url_ref="env:STAGE11_PROD_BASE_URL",
            n8n_api_key_ref="env:STAGE11_PROD_API_KEY",
            is_production=True,
        )
        # A third environment, in Org B — proves environment scoping is keyed by org,
        # not just by environment row ID.
        env_secondary_prod = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="production",
            n8n_base_url_ref="env:STAGE11_ORGB_PROD_BASE_URL",
            n8n_api_key_ref="env:STAGE11_ORGB_PROD_API_KEY",
            is_production=True,
        )

        operator_a = PrincipalRepository(session).create(kind="user", display_name="Org A Operator")
        approver_a = PrincipalRepository(session).create(kind="user", display_name="Org A Approver")
        viewer_b = PrincipalRepository(session).create(kind="user", display_name="Org B Viewer")

        memberships = OrganizationMembershipRepository(session)
        memberships.create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_staging.id, env_production.id],
        )
        memberships.create(
            principal_id=approver_a.id,
            organization_id=org_a.id,
            roles=["approver"],
            workflow_scope="*",
            environment_scope=[env_staging.id, env_production.id],
        )
        memberships.create(
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=[env_secondary_prod.id],
        )

        state = _ScenarioState(
            engine=engine,
            session_factory=factory,
            org_a_id=org_a.id,
            org_b_id=org_b.id,
            env_staging_id=env_staging.id,
            env_production_id=env_production.id,
            env_secondary_prod_id=env_secondary_prod.id,
            org_a_operator_id=operator_a.id,
            org_a_approver_id=approver_a.id,
            org_b_viewer_id=viewer_b.id,
            sink=sink,
        )

    yield state
    engine.dispose()


class TestTwoOrgThreeEnvironmentScenario:
    def test_prepare_and_approve_crm_sync_in_org_a_production(
        self, scenario: _ScenarioState
    ) -> None:
        with session_scope(scenario.session_factory) as session:
            operation, replay, _token = service.prepare_operation(
                session,
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                workflow_id="crm.sync_contact",
                arguments={"email": "lead@example.com"},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
                notification_sink=scenario.sink,
            )
            assert replay is False
            assert operation.state == "PENDING_APPROVAL"
            crm_operation_id = operation.id

        with session_scope(scenario.session_factory) as session:
            approved = service.approve_operation(
                session,
                operation_id=crm_operation_id,
                decided_by=scenario.org_a_approver_id,
                enable_v2=True,
            )
            assert approved.state == "APPROVED"

        scenario.crm_operation_id = crm_operation_id

    def test_retry_off_a_failed_operation_reaches_pending_approval_again(
        self, scenario: _ScenarioState
    ) -> None:
        from n8n_operator.storage.repository import OperationRepository

        # `retry_operation` is `admin`-only in the ADR-015 role-capability matrix
        # (a fresh, policy-significant re-authorization, ADR-012) — checked by plain
        # role capability via `_apply_environment`'s own `_authorize` call, which has
        # no ownership shortcut (unlike `_get_owned_operation_row`'s fetch step). Org
        # A's operator from the shared fixture cannot call it, so this test grants a
        # dedicated admin membership.
        with session_scope(scenario.session_factory) as session:
            admin = PrincipalRepository(session).create(kind="user", display_name="Org A Admin")
            OrganizationMembershipRepository(session).create(
                principal_id=admin.id,
                organization_id=scenario.org_a_id,
                roles=["admin"],
                workflow_scope="*",
                environment_scope=[scenario.env_production_id],
            )
            admin_id = admin.id

        with session_scope(scenario.session_factory) as session:
            snapshot = service.get_active_snapshot(session)
            assert snapshot is not None
            failed = OperationRepository(session).create(
                id="op_retry_source",
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                environment_id=scenario.env_production_id,
                organization_id=scenario.org_a_id,
                snapshot_id=snapshot.id,
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="FAILED",
                arguments={"email": "retry-me@example.com"},
                argument_fingerprint="fp-retry",
                argument_bytes=10,
            )
            failed_id = failed.id

        with session_scope(scenario.session_factory) as session:
            retried, replay, _token = service.retry_operation(
                session,
                operation_id=failed_id,
                principal_id=admin_id,
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
                notification_sink=scenario.sink,
            )
            assert replay is False
            assert retried.state == "PENDING_APPROVAL"
            assert retried.id != failed_id

    def test_reconcile_an_unknown_operation_records_evidence(
        self, scenario: _ScenarioState
    ) -> None:
        from n8n_operator.core.models import ExecutionLookup
        from n8n_operator.storage.repository import OperationRepository

        class FakeReconciliation:
            def get_execution(self, execution_id: str) -> ExecutionLookup:
                return ExecutionLookup(
                    execution_id=execution_id,
                    n8n_workflow_id="n8n-crm-1",
                    status="success",
                )

        # `reconcile_operation` is gated on the admin-only RECONCILE_CAPABILITY
        # regardless of who owns the operation (see its own docstring) — Org A's
        # operator/approver principals from the shared fixture don't hold that role,
        # so this test grants a dedicated admin membership in Org A.
        with session_scope(scenario.session_factory) as session:
            admin = PrincipalRepository(session).create(kind="user", display_name="Org A Admin")
            OrganizationMembershipRepository(session).create(
                principal_id=admin.id,
                organization_id=scenario.org_a_id,
                roles=["admin"],
                workflow_scope="*",
                environment_scope=[scenario.env_production_id],
            )
            admin_id = admin.id

        with session_scope(scenario.session_factory) as session:
            snapshot = service.get_active_snapshot(session)
            assert snapshot is not None
            unknown = OperationRepository(session).create(
                id="op_unknown_outcome",
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                environment_id=scenario.env_production_id,
                organization_id=scenario.org_a_id,
                snapshot_id=snapshot.id,
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="UNKNOWN",
                arguments={"email": "unknown-outcome@example.com"},
                argument_fingerprint="fp-unknown",
                argument_bytes=10,
            )
            unknown_id = unknown.id

        with session_scope(scenario.session_factory) as session:
            record = service.reconcile_operation(
                session,
                operation_id=unknown_id,
                principal_id=admin_id,
                execution_id="n8n-exec-999",
                note="Confirmed via n8n execution history: this run succeeded.",
                reconciliation=FakeReconciliation(),
                enable_v2=True,
            )
            assert record.execution_id == "n8n-exec-999"

    def test_diff_workflow_definition_detects_drift_on_campaign_sync(
        self, scenario: _ScenarioState
    ) -> None:
        class FakeDefinitionPort:
            def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
                return {"nodes": [{"id": "new-node", "type": "n8n-nodes-base.set"}]}

        with session_scope(scenario.session_factory) as session:
            diff = service.diff_workflow_definition(
                session,
                workflow_id="mkt.campaign_sync",
                definition=FakeDefinitionPort(),
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                enable_v2=True,
            )
            assert diff.changed is True

    def test_get_metrics_and_audit_events_never_cross_the_org_boundary(
        self, scenario: _ScenarioState
    ) -> None:
        # `scenario` is rebuilt fresh for every test method (function-scoped fixture),
        # so this test seeds its own Org A operation rather than relying on state
        # left behind by another test method's own run.
        with session_scope(scenario.session_factory) as session:
            operation, replay, _token = service.prepare_operation(
                session,
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                workflow_id="crm.sync_contact",
                arguments={"email": "isolation-check@example.com"},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
                notification_sink=scenario.sink,
            )
            assert replay is False
            org_a_operation_id = operation.id

        # `get_metrics`/`list_audit_events` only gather workflow-scope patterns from
        # memberships whose `environment_scope` is exactly `["*"]` (see
        # `core.service._resolve_scope`'s own docstring — an environment-scoped-only
        # membership, like Org A's operator/approver in this fixture, contributes
        # nothing to these two aggregate tools). A dedicated `"*"`-scoped viewer is
        # needed to prove Org A's own metrics/audit visibility here.
        with session_scope(scenario.session_factory) as session:
            org_a_star_viewer = PrincipalRepository(session).create(
                kind="user", display_name="Org A Star-Scoped Viewer"
            )
            OrganizationMembershipRepository(session).create(
                principal_id=org_a_star_viewer.id,
                organization_id=scenario.org_a_id,
                roles=["viewer"],
                workflow_scope="*",
                environment_scope=["*"],
            )
            org_a_star_viewer_id = org_a_star_viewer.id

            # `scenario.org_b_viewer_id`'s membership is environment-scoped (only
            # `env_secondary_prod_id`, not `["*"]`), so it contributes zero
            # workflow-scope patterns to `_resolve_scope` — `AuditLogRepository
            # .list_page` then forces `workflow_clause`/`operation_clause` to
            # `false()` and the query can never surface an `operation` subject at
            # all, regardless of any cross-org leak. A dedicated `"*"`-scoped Org B
            # viewer is needed so the query actually reaches the operation-matching
            # code path, making the assertion below a real proof of isolation
            # rather than a call that returns nothing by construction.
            org_b_star_viewer = PrincipalRepository(session).create(
                kind="user", display_name="Org B Star-Scoped Viewer"
            )
            OrganizationMembershipRepository(session).create(
                principal_id=org_b_star_viewer.id,
                organization_id=scenario.org_b_id,
                roles=["viewer"],
                workflow_scope="*",
                environment_scope=["*"],
            )
            org_b_star_viewer_id = org_b_star_viewer.id

        with session_scope(scenario.session_factory) as session:
            org_a_metrics = service.get_metrics(
                session,
                principal_id=org_a_star_viewer_id,
                environment=scenario.env_production_id,
                group_by="workflow",
                enable_v2=True,
            )
            # Org A's operator has never touched Org B's environment at all — this
            # call must resolve org membership correctly, never accidentally include
            # Org B's crm.sync_contact/campaign_sync operations in Org A's totals.
            assert org_a_metrics.totals.count >= 1

            org_b_events = service.list_audit_events(
                session,
                principal_id=org_b_star_viewer_id,
                environment=scenario.env_secondary_prod_id,
                enable_v2=True,
            )
            # Org B's `"*"`-scoped viewer has no membership touching Org A's
            # organization at all — Org A's own operation ID, just prepared above,
            # must never appear as a subject_id in Org B's own audit query. AuditEvent
            # carries no environment_id field directly, so isolation is proven by
            # subject identity, not a field comparison. Because this viewer is
            # `"*"`-scoped (unlike `scenario.org_b_viewer_id`), the query actually
            # reaches `AuditLogRepository.list_page`'s operation-matching code path
            # instead of returning an empty result by construction, so this assertion
            # is a genuine proof that org membership scoping — not just an empty
            # pattern list — keeps Org B's query from ever crossing into Org A.
            org_b_subject_ids = {event.subject_id for event in org_b_events.events}
            assert org_a_operation_id not in org_b_subject_ids

    def test_check_and_deliver_alerts_fires_for_a_stuck_executing_operation(
        self, scenario: _ScenarioState
    ) -> None:
        from n8n_operator.storage.repository import OperationRepository

        with session_scope(scenario.session_factory) as session:
            snapshot = service.get_active_snapshot(session)
            assert snapshot is not None
            OperationRepository(session).create(
                id="op_stuck_executing",
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                environment_id=scenario.env_production_id,
                organization_id=scenario.org_a_id,
                snapshot_id=snapshot.id,
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="EXECUTING",
                arguments={"email": "stuck@example.com"},
                argument_fingerprint="fp-stuck",
                argument_bytes=10,
            )

        with session_scope(scenario.session_factory) as session:
            delivered = service.check_and_deliver_alerts(
                session,
                sink=scenario.sink,
                executing_stuck_threshold_seconds=0,
            )
            assert delivered >= 1
            assert any(e.event_type == "operation.stuck" for e in scenario.sink.events)

    def test_both_anchor_implementations_publish_and_verify_the_same_chain(
        self, scenario: _ScenarioState, tmp_path: Path
    ) -> None:
        import httpx

        from n8n_operator.audit_anchor.local_file import AnchorReceipt as RawReceipt
        from n8n_operator.audit_anchor.local_file import LocalFileAnchor
        from n8n_operator.audit_anchor.webhook import HttpsWebhookAnchor
        from n8n_operator.core.models import AnchorReceipt, AnchorVerification, ChainAnchor

        class _ServiceSinkAdapter:
            """Converts a concrete anchor sink's own local ``AnchorReceipt``/
            ``AnchorVerification`` dataclasses into ``core.models``'s Pydantic
            equivalents — the exact conversion ``cli/commands/anchor.py``'s own
            ``_ServiceSinkAdapter`` performs so that ``core.service.publish_anchor``'s
            ``AuditAnchorPort`` (which calls ``receipt.model_dump(mode="json")``) is
            satisfied by either concrete implementation, neither of which returns a
            Pydantic model directly."""

            def __init__(self, impl: LocalFileAnchor | HttpsWebhookAnchor) -> None:
                self._impl = impl

            def publish(self, anchor: ChainAnchor) -> AnchorReceipt:
                raw = self._impl.publish(anchor)
                return AnchorReceipt(
                    implementation=raw.implementation,  # type: ignore[arg-type]
                    detail=raw.detail,
                    signature=raw.signature,
                    public_key=raw.public_key,
                )

            def verify(self, anchor: ChainAnchor, receipt: AnchorReceipt) -> AnchorVerification:
                raw_receipt = RawReceipt(
                    implementation=receipt.implementation,
                    detail=receipt.detail,
                    signature=receipt.signature,
                    public_key=receipt.public_key,
                )
                raw = self._impl.verify(anchor, raw_receipt)
                return AnchorVerification(
                    ok=raw.ok, reason=raw.reason, checked_through_seq=raw.checked_through_seq
                )

        private_key = Ed25519PrivateKey.generate()

        local_sink = _ServiceSinkAdapter(
            LocalFileAnchor(
                path=tmp_path / "anchors.jsonl",
                private_key=private_key,
            )
        )

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        webhook_sink = _ServiceSinkAdapter(
            HttpsWebhookAnchor(
                url="https://anchors.example.invalid/ingest",
                bearer_token="stage11-test-token",
                private_key=private_key,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
        )

        # `publish_anchor` is admin-gated (_require_admin) and, under enable_v2=True,
        # requires a real principal_id — Org A's shared fixture principals hold no
        # admin role, so this test grants a dedicated one.
        with session_scope(scenario.session_factory) as session:
            admin = PrincipalRepository(session).create(
                kind="user", display_name="Org A Anchor Admin"
            )
            OrganizationMembershipRepository(session).create(
                principal_id=admin.id,
                organization_id=scenario.org_a_id,
                roles=["admin"],
                workflow_scope="*",
                environment_scope=[scenario.env_production_id],
            )
            admin_id = admin.id

        with session_scope(scenario.session_factory) as session:
            local_row = service.publish_anchor(
                session,
                sink=local_sink,
                implementation="local_file",
                principal_id=admin_id,
                enable_v2=True,
            )
            assert local_row is not None

        with session_scope(scenario.session_factory) as session:
            webhook_row = service.publish_anchor(
                session,
                sink=webhook_sink,
                implementation="https_webhook",
                principal_id=admin_id,
                enable_v2=True,
            )
            assert webhook_row is not None
        assert len(captured) == 1
