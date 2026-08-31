"""Parameterized cross-organization isolation guard (Stage 11 security review) —
every v2 read surface audited for this stage's threat-model entry (see
``docs/THREAT_MODEL.md``'s new T-entry and ``docs/evidence/stage11-security-review.md``)
runs against the *same* two-organization, three-environment, shared-workflow-id
fixture, asserted in one parametrized test.

This exists to be extended, not just to pass today: a future v2 read surface that
queries operation/audit/environment data belongs in ``SURFACES`` below the moment it
exists, so it gets this exact isolation check for free rather than needing its own
bespoke cross-org test written from scratch (the same mistake that let the confirmed
``AuditLogRepository.list_page`` leak this stage fixed go unnoticed for as long as it
did — no test in this codebase, before this stage, ever put two organizations sharing
one workflow id in front of *any* v2 read path at once).

Every check function below takes ``(session, scenario)`` and asserts, in whatever
shape is native to that surface, that Org B's own principal — calling as Org B, in
Org B's own environment — never sees Org A's operation, environment, or any data
derived from them. A check that finds a leak raises ``AssertionError`` (a normal
failed test), not a custom exception — pytest's ordinary failure reporting is exactly
what a maintainer debugging a broken isolation guard needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.errors import OperationNotFoundError
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: tenant-isolation-matrix
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
    description: Shared across both organizations in this fixture.
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
""".format(hash_a="a" * 64)


class ReadyPreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@dataclass
class IsolationScenario:
    session_factory: sessionmaker[Session]
    org_a_id: str
    org_b_id: str
    env_a_id: str
    env_b_id: str
    viewer_a_id: str
    viewer_b_id: str
    operator_a_id: str
    operator_b_id: str
    op_a_id: str
    op_b_id: str


@pytest.fixture
def scenario(session_factory: sessionmaker[Session], tmp_path: Path) -> IsolationScenario:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
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
        operator_b = PrincipalRepository(session).create(kind="user", display_name="Org B Operator")
        viewer_b = PrincipalRepository(session).create(kind="user", display_name="Org B Viewer")
        memberships = OrganizationMembershipRepository(session)
        memberships.create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_a.id],
        )
        memberships.create(
            principal_id=viewer_a.id,
            organization_id=org_a.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        memberships.create(
            principal_id=operator_b.id,
            organization_id=org_b.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_b.id],
        )
        memberships.create(
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        ids = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "env_a_id": env_a.id,
            "env_b_id": env_b.id,
            "operator_a_id": operator_a.id,
            "viewer_a_id": viewer_a.id,
            "operator_b_id": operator_b.id,
            "viewer_b_id": viewer_b.id,
        }

    with session_scope(session_factory) as session:
        op_a, _replay, _token = service.prepare_operation(
            session,
            principal_id=ids["operator_a_id"],
            environment=ids["env_a_id"],
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=ReadyPreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        op_a_id = op_a.id
    with session_scope(session_factory) as session:
        op_b, _replay, _token = service.prepare_operation(
            session,
            principal_id=ids["operator_b_id"],
            environment=ids["env_b_id"],
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=ReadyPreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        op_b_id = op_b.id

    return IsolationScenario(
        session_factory=session_factory,
        org_a_id=ids["org_a_id"],
        org_b_id=ids["org_b_id"],
        env_a_id=ids["env_a_id"],
        env_b_id=ids["env_b_id"],
        viewer_a_id=ids["viewer_a_id"],
        viewer_b_id=ids["viewer_b_id"],
        operator_a_id=ids["operator_a_id"],
        operator_b_id=ids["operator_b_id"],
        op_a_id=op_a_id,
        op_b_id=op_b_id,
    )


def _check_list_audit_events(session: Session, s: IsolationScenario) -> None:
    page = service.list_audit_events(
        session, principal_id=s.viewer_b_id, environment=s.env_b_id, limit=100, enable_v2=True
    )
    subject_ids = {e.subject_id for e in page.events if e.subject_type == "operation"}
    assert s.op_a_id not in subject_ids
    assert s.op_b_id in subject_ids


def _check_get_metrics(session: Session, s: IsolationScenario) -> None:
    result = service.get_metrics(
        session,
        principal_id=s.viewer_b_id,
        environment=s.env_b_id,
        group_by="workflow",
        enable_v2=True,
    )
    # Org B prepared exactly one operation; if Org A's own operation against the same
    # shared workflow id ever counted, this total would be 2.
    assert result.totals.count == 1


def _check_list_operations(session: Session, s: IsolationScenario) -> None:
    rows = service.list_operations(
        session, principal_id=s.viewer_b_id, environment=s.env_b_id, limit=100, enable_v2=True
    )
    ids = {row.id for row in rows}
    assert s.op_a_id not in ids
    assert s.op_b_id in ids


def _check_get_execution_result(session: Session, s: IsolationScenario) -> None:
    with pytest.raises(OperationNotFoundError):
        service.get_execution_result(
            session, operation_id=s.op_a_id, principal_id=s.viewer_b_id, enable_v2=True
        )


def _check_get_approval_status(session: Session, s: IsolationScenario) -> None:
    with pytest.raises(OperationNotFoundError):
        service.get_approval_status(
            session, operation_id=s.op_a_id, principal_id=s.viewer_b_id, enable_v2=True
        )


def _check_list_reconciliation_events(session: Session, s: IsolationScenario) -> None:
    with pytest.raises(OperationNotFoundError):
        service.list_reconciliation_events(
            session, operation_id=s.op_a_id, principal_id=s.viewer_b_id, enable_v2=True
        )


def _check_list_environments(session: Session, s: IsolationScenario) -> None:
    summaries = service.list_environments(session, principal_id=s.viewer_b_id)
    environment_ids = {summary.environment_id for summary in summaries}
    assert s.env_a_id not in environment_ids
    assert s.env_b_id in environment_ids


SURFACES: list[tuple[str, Any]] = [
    ("list_audit_events", _check_list_audit_events),
    ("get_metrics", _check_get_metrics),
    ("list_operations", _check_list_operations),
    ("get_execution_result", _check_get_execution_result),
    ("get_approval_status", _check_get_approval_status),
    ("list_reconciliation_events", _check_list_reconciliation_events),
    ("list_environments", _check_list_environments),
]


@pytest.mark.integration
@pytest.mark.parametrize("name,check", SURFACES, ids=[name for name, _ in SURFACES])
def test_v2_read_surface_never_crosses_the_org_boundary(
    scenario: IsolationScenario, name: str, check: Any
) -> None:
    with session_scope(scenario.session_factory) as session:
        check(session, scenario)
