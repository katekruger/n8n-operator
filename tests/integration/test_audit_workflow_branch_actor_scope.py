"""Regression test for the Stage 11 adversarial-review finding tracked in
``docs/evidence/stage11-security-review-addendum.md``: ``AuditLogRepository.list_page``'s
``subject_type="workflow"`` branch is filtered only by ``workflow_id_like_patterns`` —
no ``environment_id``/``organization_id`` conjunct, unlike the ``subject_type="operation"``
branch right next to it. That branch is correct for workflow *definitions* (they are
global, not organization-namespaced — see ``list_page``'s own docstring), but it also
carries ``operation.prepare_denied`` events, which ``core.service._prepare_operation_impl``
writes with ``subject_type="workflow"`` and ``actor=principal_id`` (the *denied caller's*
own principal id, per ``service.py`` ~line 1835-1842). Those two facts combined mean a
viewer in one organization, holding a wildcard workflow scope, can read another
organization's principal ULIDs and denial timestamps for any workflow id the two
organizations happen to share — a narrower instance of the same cross-tenant
identifier/timing leak T-66 already covers for the operation branch, but left open here.

This test reuses the two-organization, shared-workflow-id fixture pattern from
``test_tenant_isolation_matrix.py``, drives Org A's operator into a real
``operation.prepare_denied`` (an oversized-argument rejection), and then asserts Org B's
viewer — reading with a wildcard workflow scope — cannot see Org A's operator's
principal id anywhere in the page. It is expected to FAIL today (xfail, strict) because
the leak is real, not hypothetical; a future fix flips this to a normal passing test.
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
from n8n_operator.errors import ArgumentsTooLargeError
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: audit-workflow-branch-actor-scope
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
      max_argument_bytes: 16
""".format(hash_a="a" * 64)


class ReadyPreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@dataclass
class Scenario:
    session_factory: sessionmaker[Session]
    env_a_id: str
    env_b_id: str
    viewer_b_id: str
    operator_a_id: str


@pytest.fixture
def scenario(session_factory: sessionmaker[Session], tmp_path: Path) -> Scenario:
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
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        ids = {
            "env_a_id": env_a.id,
            "env_b_id": env_b.id,
            "operator_a_id": operator_a.id,
            "viewer_b_id": viewer_b.id,
        }

    # Org A's operator gets denied: the workflow's own `max_argument_bytes: 16` limit
    # rejects this payload, which writes a `subject_type="workflow"`,
    # `actor=operator_a_id` `operation.prepare_denied` audit row (service.py's
    # `_prepare_operation_impl`, the `except (ArgumentsTooLargeError, ...)` branch).
    with session_scope(session_factory) as session, pytest.raises(ArgumentsTooLargeError):
        service.prepare_operation(
            session,
            principal_id=ids["operator_a_id"],
            environment=ids["env_a_id"],
            workflow_id="crm.sync_contact",
            arguments={"padding": "x" * 256},
            preflight=ReadyPreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )

    return Scenario(
        session_factory=session_factory,
        env_a_id=ids["env_a_id"],
        env_b_id=ids["env_b_id"],
        viewer_b_id=ids["viewer_b_id"],
        operator_a_id=ids["operator_a_id"],
    )


@pytest.mark.integration
@pytest.mark.xfail(
    reason="tracked in docs/evidence/stage11-security-review-addendum.md, not yet fixed",
    strict=True,
)
def test_workflow_branch_denial_actor_not_visible_across_organizations(
    scenario: Scenario,
) -> None:
    with session_scope(scenario.session_factory) as session:
        page = service.list_audit_events(
            session,
            principal_id=scenario.viewer_b_id,
            environment=scenario.env_b_id,
            limit=100,
            enable_v2=True,
        )
    actors = {e.actor for e in page.events}
    # Org B's viewer should never learn that Org A's operator attempted (and was
    # denied) the shared workflow id. Today, it does.
    assert scenario.operator_a_id not in actors
