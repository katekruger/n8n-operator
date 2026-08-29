"""``core.service``'s v2 authorization wiring (Stage 03, ADR-015), against a real
database — the named "Required proof" scenarios from the Stage 03 spec that need a
real principal/organization/membership graph rather than the pure evaluator alone
(``tests/property/test_rbac_matrix.py``/``test_no_enumeration.py`` cover the pure
side): mid-session revocation, conflicting/multi-organization grants, disabled
principals, and the CLI-only ``admin``-gated audit commands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.errors import (
    InsufficientRoleError,
    OperationNotFoundError,
    WorkflowNotFoundError,
)
from n8n_operator.storage.repository import (
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: authz-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
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
      properties: {{}}
      additionalProperties: false
    output:
      include_node_trace: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: billing.charge_card
    n8n_workflow_id: n8n-2
    title: Charge card
    description: Irreversible.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: high
    side_effects: irreversible
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], registry_path: Path) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


def _prepare(
    session_factory: sessionmaker[Session],
    *,
    principal_id: str,
    workflow_id: str = "crm.sync_contact",
) -> str:
    with session_scope(session_factory) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment="default",
            workflow_id=workflow_id,
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        return operation.id


# ----------------------------------------------------------------------------------
# Mid-session revocation — role/scope changes, not just principal disablement.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_removing_a_membership_denies_the_very_next_call(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        alice = PrincipalRepository(session).create(kind="user", display_name="Alice")
        OrganizationMembershipRepository(session).create(
            principal_id=alice.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        alice_id = alice.id

    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=alice_id, enable_v2=True
        )
        assert detail.workflow_id == "crm.sync_contact"

    with session_scope(loaded) as session:
        membership = OrganizationMembershipRepository(session).get_active(
            principal_id=alice_id, organization_id=OrganizationRepository(session).list()[0].id
        )
        assert membership is not None
        OrganizationMembershipRepository(session).remove(membership.id)

    with session_scope(loaded) as session, pytest.raises(WorkflowNotFoundError):
        service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=alice_id, enable_v2=True
        )


@pytest.mark.integration
def test_narrowing_workflow_scope_mid_session_denies_the_very_next_call(
    loaded: sessionmaker[Session],
) -> None:
    """Role/scope changes are re-checked live, never cached — the same discipline
    Stage 02 established for principal disablement, extended here to a membership row
    that still exists but was *updated* (removed and re-created with a narrower scope,
    since a membership row is immutable except for ``removed_at``)."""
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        bob = PrincipalRepository(session).create(kind="user", display_name="Bob")
        OrganizationMembershipRepository(session).create(
            principal_id=bob.id, organization_id=org.id, roles=["operator"], workflow_scope="*"
        )
        bob_id, org_id = bob.id, org.id

    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session, workflow_id="billing.charge_card", principal_id=bob_id, enable_v2=True
        )
        assert detail.workflow_id == "billing.charge_card"

    with session_scope(loaded) as session:
        membership = OrganizationMembershipRepository(session).get_active(
            principal_id=bob_id, organization_id=org_id
        )
        assert membership is not None
        OrganizationMembershipRepository(session).remove(membership.id)
        OrganizationMembershipRepository(session).create(
            principal_id=bob_id, organization_id=org_id, roles=["operator"], workflow_scope="crm.*"
        )

    with session_scope(loaded) as session, pytest.raises(WorkflowNotFoundError):
        service.describe_workflow(
            session, workflow_id="billing.charge_card", principal_id=bob_id, enable_v2=True
        )
    with session_scope(loaded) as session:
        # crm.* is still covered by the narrowed grant.
        service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=bob_id, enable_v2=True
        )


# ----------------------------------------------------------------------------------
# Conflicting / multi-organization grants — union across memberships.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_principal_in_two_organizations_is_authorized_by_either_grant_independently(
    loaded: sessionmaker[Session],
) -> None:
    """ADR-015: trying each membership's own, self-contained grant independently is not
    the "union across grants" the ADR rejects — that alternative was about mixing
    fields *across* grants. A principal with `operator` on `crm.*` in org A and
    `viewer` on `billing.*` in org B is authorized to `prepare_operation` on
    `crm.sync_contact` (org A's grant, on its own terms) but not on
    `billing.charge_card` (org B only grants `viewer` there, and org A's grant does not
    cover that workflow at all) — never "operator on billing.* by combining the two"."""
    with session_scope(loaded) as session:
        org_a = OrganizationRepository(session).create(name="Org A")
        org_b = OrganizationRepository(session).create(name="Org B")
        carol = PrincipalRepository(session).create(kind="user", display_name="Carol")
        OrganizationMembershipRepository(session).create(
            principal_id=carol.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="crm.*",
        )
        OrganizationMembershipRepository(session).create(
            principal_id=carol.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="billing.*",
        )
        carol_id = carol.id

    with session_scope(loaded) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=carol_id,
            environment="default",
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        assert operation.workflow_id == "crm.sync_contact"

    with session_scope(loaded) as session, pytest.raises(WorkflowNotFoundError):
        # billing.* grant is viewer-only in org B; org A's operator grant does not
        # cover billing.* at all — neither grant, alone, authorizes this.
        service.prepare_operation(
            session,
            principal_id=carol_id,
            environment="default",
            workflow_id="billing.charge_card",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )


# ----------------------------------------------------------------------------------
# Approval self-grants.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_an_approver_may_never_decide_their_own_operation(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        dana = PrincipalRepository(session).create(kind="user", display_name="Dana")
        OrganizationMembershipRepository(session).create(
            principal_id=dana.id,
            organization_id=org.id,
            roles=["operator", "approver"],
            workflow_scope="*",
        )
        dana_id = dana.id

    operation_id = _prepare(loaded, principal_id=dana_id, workflow_id="billing.charge_card")

    with session_scope(loaded) as session, pytest.raises(OperationNotFoundError):
        service.approve_operation(
            session, operation_id=operation_id, decided_by=dana_id, enable_v2=True
        )


@pytest.mark.integration
def test_a_different_approver_may_decide_it(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        requester = PrincipalRepository(session).create(kind="user", display_name="Requester")
        approver = PrincipalRepository(session).create(kind="user", display_name="Approver")
        OrganizationMembershipRepository(session).create(
            principal_id=requester.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="*",
        )
        OrganizationMembershipRepository(session).create(
            principal_id=approver.id, organization_id=org.id, roles=["approver"], workflow_scope="*"
        )
        requester_id, approver_id = requester.id, approver.id

    operation_id = _prepare(loaded, principal_id=requester_id, workflow_id="billing.charge_card")

    with session_scope(loaded) as session:
        approved = service.approve_operation(
            session, operation_id=operation_id, decided_by=approver_id, enable_v2=True
        )
        assert approved.state == "APPROVED"


@pytest.mark.integration
def test_a_viewer_without_the_approver_role_cannot_decide(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        requester = PrincipalRepository(session).create(kind="user", display_name="Requester")
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=requester.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="*",
        )
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        requester_id, viewer_id = requester.id, viewer.id

    operation_id = _prepare(loaded, principal_id=requester_id, workflow_id="billing.charge_card")

    with session_scope(loaded) as session, pytest.raises(OperationNotFoundError):
        service.approve_operation(
            session, operation_id=operation_id, decided_by=viewer_id, enable_v2=True
        )


# ----------------------------------------------------------------------------------
# Audit commands require `admin` (system-wide, not workflow-scoped).
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_verify_requires_admin(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        operator = PrincipalRepository(session).create(kind="user", display_name="Op")
        admin = PrincipalRepository(session).create(kind="user", display_name="Admin")
        OrganizationMembershipRepository(session).create(
            principal_id=operator.id, organization_id=org.id, roles=["operator"], workflow_scope="*"
        )
        OrganizationMembershipRepository(session).create(
            principal_id=admin.id, organization_id=org.id, roles=["admin"], workflow_scope="*"
        )
        operator_id, admin_id = operator.id, admin.id

    with session_scope(loaded) as session, pytest.raises(InsufficientRoleError):
        service.verify_audit_chain(session, principal_id=operator_id, enable_v2=True)

    with session_scope(loaded) as session:
        result = service.verify_audit_chain(session, principal_id=admin_id, enable_v2=True)
        assert result.ok


@pytest.mark.integration
def test_audit_export_requires_admin(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        viewer_id = viewer.id

    with session_scope(loaded) as session, pytest.raises(InsufficientRoleError):
        service.export_audit_record(session, principal_id=viewer_id, enable_v2=True)


# ----------------------------------------------------------------------------------
# Pagination — a filter/scope must apply before LIMIT, not after.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_operations_scope_filter_applies_before_the_page_limit(
    loaded: sessionmaker[Session],
) -> None:
    """A caller whose scope covers only `crm.*` must see a full page of `crm.*`
    operations even when `billing.*` operations were interleaved chronologically — if
    the scope filter were applied *after* `LIMIT`, a page could come back short of
    real, visible rows sitting just past the (pre-filter) cutoff."""
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        eve = PrincipalRepository(session).create(kind="user", display_name="Eve")
        admin = PrincipalRepository(session).create(kind="user", display_name="Admin")
        OrganizationMembershipRepository(session).create(
            principal_id=eve.id, organization_id=org.id, roles=["operator"], workflow_scope="crm.*"
        )
        OrganizationMembershipRepository(session).create(
            principal_id=admin.id, organization_id=org.id, roles=["admin"], workflow_scope="*"
        )
        eve_id, admin_id = eve.id, admin.id

    crm_ids = []
    for _ in range(3):
        crm_ids.append(_prepare(loaded, principal_id=eve_id, workflow_id="crm.sync_contact"))
        # Interleave a billing.* operation from a different principal, out of scope
        # for Eve's crm.* grant but real and chronologically between the crm.* ones.
        with session_scope(loaded) as session:
            service.prepare_operation(
                session,
                principal_id=admin_id,
                environment="default",
                workflow_id="billing.charge_card",
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
            )

    with session_scope(loaded) as session:
        page = service.list_operations(session, principal_id=eve_id, limit=3, enable_v2=True)
    returned_ids = {op.id for op in page}
    assert returned_ids == set(crm_ids)
    assert all(op.workflow_id == "crm.sync_contact" for op in page)
