"""N-of-M team approval (ADR-017; stage 05) against a real database — quorum
tallying in ``approve_operation``/``reject_operation``, ``request_approval``,
``get_approval_status``, and the edge cases ADR-017's own sections name.

Mirrors ``tests/integration/test_approval_service.py``'s v1 coverage, extended to the
v2 quorum branch (``enable_v2=True``, a registry entry with ``limits.quorum_count >
1``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import DeliveryOutcome, NotificationEvent, PreflightResult
from n8n_operator.errors import (
    ApprovalAlreadyDecidedError,
    ApproverNotInPolicyError,
    OperationNotFoundError,
)
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: quorum-test
workflows:
  - id: crm.bulk_update_stage
    n8n_workflow_id: n8n-1
    title: Bulk-update deal stage
    description: Production-only, two-approver.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: high
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties:
        note: {{type: string}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
      quorum_count: 2
""".format(hash_a="a" * 64)

_SECRET_ARGUMENT_VALUE = "sk_live_totally_secret_deal_note_9f3a1c"


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


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
def world(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    """An org with a requester and three qualifying approvers, in one production
    environment, plus the loaded quorum-2 registry."""
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
            is_production=True,
        )
        requester = PrincipalRepository(session).create(kind="user", display_name="Requester")
        approver_a = PrincipalRepository(session).create(kind="user", display_name="Approver A")
        approver_b = PrincipalRepository(session).create(kind="user", display_name="Approver B")
        approver_c = PrincipalRepository(session).create(kind="user", display_name="Approver C")
        memberships = OrganizationMembershipRepository(session)
        memberships.create(principal_id=requester.id, organization_id=org.id, roles=["operator"])
        memberships.create(principal_id=approver_a.id, organization_id=org.id, roles=["approver"])
        # approver_b holds a role set that grants approval via `admin`, not just the
        # dedicated `approver` role — both carry APPROVE_REJECT_CAPABILITY and must
        # count identically toward the snapshot (only one active membership per
        # principal+org is possible at all — the DB's own uniqueness constraint — so
        # this is the one way multiple *qualifying roles* can coexist on one grant).
        memberships.create(
            principal_id=approver_b.id, organization_id=org.id, roles=["admin", "approver"]
        )
        memberships.create(principal_id=approver_c.id, organization_id=org.id, roles=["approver"])
        return {
            "org_id": org.id,
            "env_id": env.id,
            "requester_id": requester.id,
            "approver_a": approver_a.id,
            "approver_b": approver_b.id,
            "approver_c": approver_c.id,
        }


def _prepare(
    session_factory: sessionmaker[Session],
    world: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
) -> str:
    with session_scope(session_factory) as session:
        operation, _, _ = service.prepare_operation(
            session,
            principal_id=world["requester_id"],
            environment=world["env_id"],
            workflow_id="crm.bulk_update_stage",
            arguments=arguments or {},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        assert operation.state == "PENDING_APPROVAL"
        return operation.id


def test_eligible_approvers_dedup_across_memberships(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """Every qualifying principal occupies exactly one snapshot slot regardless of
    which qualifying role grants it, and the requester is structurally excluded
    (ADR-017 section 1)."""
    operation_id = _prepare(session_factory, world)
    with session_scope(session_factory) as session:
        status = service.get_approval_status(
            session, operation_id=operation_id, principal_id=world["requester_id"], enable_v2=True
        )
    assert status.quorum_count == 2
    assert sorted(status.approval_policy_snapshot) == sorted(
        [world["approver_a"], world["approver_b"], world["approver_c"]]
    )
    assert world["requester_id"] not in status.approval_policy_snapshot


def test_quorum_reached_transitions_to_approved(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with session_scope(session_factory) as session:
        op = service.approve_operation(
            session,
            operation_id=operation_id,
            decided_by=world["approver_a"],
            enable_v2=True,
        )
    assert op.state == "PENDING_APPROVAL"

    with session_scope(session_factory) as session:
        op = service.approve_operation(
            session,
            operation_id=operation_id,
            decided_by=world["approver_b"],
            enable_v2=True,
        )
    assert op.state == "APPROVED"


def test_approval_already_decided_on_second_attempt(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with session_scope(session_factory) as session:
        service.approve_operation(
            session, operation_id=operation_id, decided_by=world["approver_a"], enable_v2=True
        )
    with (
        session_scope(session_factory) as session,
        pytest.raises(ApprovalAlreadyDecidedError),
    ):
        service.approve_operation(
            session, operation_id=operation_id, decided_by=world["approver_a"], enable_v2=True
        )


def test_one_reject_is_final_regardless_of_approvals_already_in(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with session_scope(session_factory) as session:
        service.approve_operation(
            session, operation_id=operation_id, decided_by=world["approver_a"], enable_v2=True
        )
    with session_scope(session_factory) as session:
        op = service.reject_operation(
            session, operation_id=operation_id, decided_by=world["approver_b"], enable_v2=True
        )
    assert op.state == "REJECTED"


def test_requester_cannot_decide_their_own_operation(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with (
        session_scope(session_factory) as session,
        pytest.raises(OperationNotFoundError),
    ):
        service.approve_operation(
            session, operation_id=operation_id, decided_by=world["requester_id"], enable_v2=True
        )


def test_non_eligible_principal_cannot_decide(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with (
        session_scope(session_factory) as session,
        pytest.raises(OperationNotFoundError),
    ):
        service.approve_operation(
            session, operation_id=operation_id, decided_by="not-an-approver", enable_v2=True
        )


def test_request_approval_rejects_an_approver_outside_the_snapshot(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    with (
        session_scope(session_factory) as session,
        pytest.raises(ApproverNotInPolicyError),
    ):
        service.request_approval(
            session,
            operation_id=operation_id,
            principal_id=world["requester_id"],
            sink=FakeSink(),
            approvers=["not-an-approver"],
        )


def test_request_approval_notifies_and_get_approval_status_reflects_it(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    sink = FakeSink()
    with session_scope(session_factory) as session:
        result = service.request_approval(
            session, operation_id=operation_id, principal_id=world["requester_id"], sink=sink
        )
    assert sorted(result.notified) == sorted(
        [world["approver_a"], world["approver_b"], world["approver_c"]]
    )
    assert len(sink.events) == 3
    for event in sink.events:
        assert event.event_type == "approval.requested"
        # Redaction (ADR-018 section 4): never operation arguments/title/description.
        assert not hasattr(event, "arguments")
        assert not hasattr(event, "title")

    with session_scope(session_factory) as session:
        op = service.approve_operation(
            session, operation_id=operation_id, decided_by=world["approver_a"], enable_v2=True
        )
    assert op.state == "PENDING_APPROVAL"

    with session_scope(session_factory) as session:
        status = service.get_approval_status(
            session, operation_id=operation_id, principal_id=world["requester_id"], enable_v2=True
        )
    assert len(status.decisions) == 1
    assert status.decisions[0].principal_id == world["approver_a"]
    assert set(status.outstanding) == {world["approver_b"], world["approver_c"]}
    assert status.ready is False


def test_unreachable_quorum_falls_through_to_ordinary_expiry(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """Removing an approver mid-flight (invariant I13: the snapshot never regains
    members, but decisions already cast survive) can leave a quorum unreachable —
    there is no special resolution path for this; it simply expires like any other
    overdue ``PENDING_APPROVAL`` operation (invariant I9)."""
    operation_id = _prepare(session_factory, world)
    with session_scope(session_factory) as session:
        service.approve_operation(
            session, operation_id=operation_id, decided_by=world["approver_a"], enable_v2=True
        )
        # approver_c is removed after the snapshot was taken — the snapshot itself
        # is untouched (I13); their slot is still in it, but they can never actually
        # cast a vote now.
        memberships = OrganizationMembershipRepository(session)
        approver_c_membership = memberships.get_active(
            principal_id=world["approver_c"], organization_id=world["org_id"]
        )
        assert approver_c_membership is not None
        memberships.remove(approver_c_membership.id)
    with session_scope(session_factory) as session:
        op = service.get_approval_status(
            session, operation_id=operation_id, principal_id=world["requester_id"], enable_v2=True
        )
    assert op.ready is False
    # No special "unreachable" state — still PENDING_APPROVAL, subject to ordinary
    # TTL expiry only.


def test_notification_payload_never_carries_the_operations_own_argument_value(
    session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """No-secrets artifact inspection (ADR-018 section 4, the stage 04 completion
    gate's CLI pattern extended to notification delivery): an operation prepared
    with a distinctive, secret-shaped argument value must never have that value
    reach any field of any ``NotificationEvent`` a real ``request_approval`` call
    produces — checked against the fully serialized event, not just the fields this
    test happens to think to check."""
    operation_id = _prepare(session_factory, world, arguments={"note": _SECRET_ARGUMENT_VALUE})
    sink = FakeSink()
    with session_scope(session_factory) as session:
        service.request_approval(
            session, operation_id=operation_id, principal_id=world["requester_id"], sink=sink
        )
    assert len(sink.events) == 3
    for event in sink.events:
        serialized = event.model_dump_json()
        assert _SECRET_ARGUMENT_VALUE not in serialized
