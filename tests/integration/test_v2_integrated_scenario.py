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
