"""Stage 05's own completion gate: the four named GTM usability proofs (BUILD_PLAN
section 12, ADR-017), driven through the real MCP tool layer exactly as
``tests/integration/test_gtm_scenarios.py`` drives stage 04's — ``request_approval``
and ``get_approval_status`` are real MCP tool calls; the actual decision crosses the
CLI/``core.service`` boundary directly (boundary B4: no MCP tool ever approves).

1. One approval for a staging CRM sync (``quorum_count: 1`` — the v2 "single-approver
   behavior is quorum_count: 1, not a different code path" case, registry.schema.py's
   own framing).
2. Two distinct approvals for a production bulk update (ARCHITECTURE.md section 11.2).
3. Marketing plus legal approval for a customer-facing campaign launch — two named
   approvers from different workflow-scoped roles both required.
4. An irreversible action that cannot be approved by its requester, even though they
   themselves hold ``approver``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import (
    DeliveryOutcome,
    DispatchOutcome,
    NotificationEvent,
    PreflightResult,
)
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: gtm-usability-stage05
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
    description: Staging enrichment — one approval.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: low
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
      quorum_count: 1
  - id: crm.bulk_update_stage
    n8n_workflow_id: n8n-2
    title: Bulk-update deal stage
    description: Production bulk update — two distinct approvals.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: high
    side_effects: external_write
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
      quorum_count: 2
  - id: mkt.launch_campaign
    n8n_workflow_id: n8n-3
    title: Launch a customer-facing campaign
    description: Marketing plus legal sign-off required.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_c}
    risk: high
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/c
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
      quorum_count: 2
  - id: billing.delete_customer_data
    n8n_workflow_id: n8n-4
    title: Permanently delete a customer's stored data
    description: Irreversible — the requester can never be their own approver.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_d}
    risk: high
    side_effects: irreversible
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/d
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
      quorum_count: 1
""".format(hash_a="a" * 64, hash_b="b" * 64, hash_c="c" * 64, hash_d="d" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class FakeHealth:
    def check(self) -> Any:
        raise NotImplementedError


class FakeDispatch:
    def dispatch(
        self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        return DispatchOutcome(
            kind="success",
            http_status=200,
            result={},
            execution_id=None,
            correlation_available=False,
        )

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        return None


class FakeDefinition:
    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        raise NotImplementedError


class FakeSink:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        self.events.append(event)
        return DeliveryOutcome(delivered=True)


def _make_server(
    session_factory: sessionmaker[Session], *, principal_id: str, sink: FakeSink
) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=FakePreflight(),
        health=FakeHealth(),
        dispatch=FakeDispatch(),
        definition=FakeDefinition(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        enable_v2=True,
        notification_sink=sink,
    )
    return MCPServer("test", tools=build_tools(deps))


async def _call(server: MCPServer[Any], name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


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


@pytest.fixture
def world(loaded: sessionmaker[Session]) -> dict[str, Any]:
    """One org, one staging and one production environment, and a small GTM-shaped
    cast: a requester, two RevOps approvers, a marketing approver, and a legal
    approver — sales.* excluded from marketing's scope, mirroring
    ``test_gtm_scenarios.py``'s own naming conventions."""
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        staging = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="staging",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        production = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
            is_production=True,
        )
        requester = PrincipalRepository(session).create(kind="user", display_name="Requester")
        revops_a = PrincipalRepository(session).create(kind="user", display_name="RevOps A")
        revops_b = PrincipalRepository(session).create(kind="user", display_name="RevOps B")
        marketing_approver = PrincipalRepository(session).create(
            kind="user", display_name="Marketing Lead"
        )
        legal_approver = PrincipalRepository(session).create(kind="user", display_name="Legal")

        memberships = OrganizationMembershipRepository(session)
        # The requester also holds `approver` (scope `*`, so it covers the
        # irreversible `billing.*` workflow too) — a principal can hold only one
        # active membership per org (the DB's own uniqueness constraint), so this is
        # the one grant that carries both roles at once. Structurally still excluded
        # from their own request's snapshot regardless (case 4).
        memberships.create(
            principal_id=requester.id, organization_id=org.id, roles=["operator", "approver"]
        )
        memberships.create(
            principal_id=revops_a.id,
            organization_id=org.id,
            roles=["approver"],
            workflow_scope="crm.*",
        )
        memberships.create(
            principal_id=revops_b.id,
            organization_id=org.id,
            roles=["approver"],
            workflow_scope="crm.*",
        )
        memberships.create(
            principal_id=marketing_approver.id,
            organization_id=org.id,
            roles=["approver"],
            workflow_scope="mkt.*",
        )
        memberships.create(
            principal_id=legal_approver.id,
            organization_id=org.id,
            roles=["approver"],
            workflow_scope="mkt.*",
        )

        return {
            "staging_id": staging.id,
            "production_id": production.id,
            "requester_id": requester.id,
            "revops_a": revops_a.id,
            "revops_b": revops_b.id,
            "marketing_approver": marketing_approver.id,
            "legal_approver": legal_approver.id,
        }


@pytest.mark.integration
async def test_one_approval_for_a_staging_crm_sync(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    sink = FakeSink()
    server = _make_server(loaded, principal_id=world["requester_id"], sink=sink)

    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="crm.sync_contact",
        arguments={},
        environment=world["staging_id"],
    )
    assert prepared["state"] == "PENDING_APPROVAL"
    operation_id = prepared["operation_id"]

    routed = await _call(server, "request_approval", operation_id=operation_id)
    assert routed["quorum_count"] == 1
    # Both RevOps principals are eligible (crm.* scope) — quorum 1 means only one
    # decision is needed to reach APPROVED, not that only one principal is notified.
    assert sorted(routed["notified"]) == sorted([world["revops_a"], world["revops_b"]])
    assert len(sink.events) == 2

    decider = routed["notified"][0]
    with session_scope(loaded) as session:
        operation = service.approve_operation(
            session, operation_id=operation_id, decided_by=decider, enable_v2=True
        )
    assert operation.state == "APPROVED"

    status = await _call(server, "get_approval_status", operation_id=operation_id)
    assert status["ready"] is True


@pytest.mark.integration
async def test_two_distinct_approvals_for_a_production_bulk_update(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """ARCHITECTURE.md section 11.2, end to end through the MCP tool layer."""
    sink = FakeSink()
    server = _make_server(loaded, principal_id=world["requester_id"], sink=sink)

    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="crm.bulk_update_stage",
        arguments={},
        environment=world["production_id"],
    )
    assert prepared["state"] == "PENDING_APPROVAL"
    operation_id = prepared["operation_id"]

    routed = await _call(server, "request_approval", operation_id=operation_id)
    assert routed["quorum_count"] == 2
    assert sorted(routed["notified"]) == sorted([world["revops_a"], world["revops_b"]])

    with session_scope(loaded) as session:
        first = service.approve_operation(
            session, operation_id=operation_id, decided_by=world["revops_a"], enable_v2=True
        )
    assert first.state == "PENDING_APPROVAL"

    mid_status = await _call(server, "get_approval_status", operation_id=operation_id)
    assert mid_status["ready"] is False
    assert mid_status["outstanding"] == [world["revops_b"]]

    with session_scope(loaded) as session:
        second = service.approve_operation(
            session, operation_id=operation_id, decided_by=world["revops_b"], enable_v2=True
        )
    assert second.state == "APPROVED"

    final_status = await _call(server, "get_approval_status", operation_id=operation_id)
    assert final_status["ready"] is True

    handle_result = await _call(
        server, "execute_operation", operation_id=operation_id, handle=operation_id
    )
    assert handle_result["operation_id"] == operation_id


@pytest.mark.integration
async def test_marketing_plus_legal_approval_for_a_campaign_launch(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    sink = FakeSink()
    server = _make_server(loaded, principal_id=world["requester_id"], sink=sink)

    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="mkt.launch_campaign",
        arguments={},
        environment=world["production_id"],
    )
    operation_id = prepared["operation_id"]

    routed = await _call(server, "request_approval", operation_id=operation_id)
    assert sorted(routed["notified"]) == sorted(
        [world["marketing_approver"], world["legal_approver"]]
    )

    with session_scope(loaded) as session:
        service.approve_operation(
            session,
            operation_id=operation_id,
            decided_by=world["marketing_approver"],
            enable_v2=True,
        )
        final = service.approve_operation(
            session, operation_id=operation_id, decided_by=world["legal_approver"], enable_v2=True
        )
    assert final.state == "APPROVED"


@pytest.mark.integration
async def test_irreversible_action_cannot_be_approved_by_its_own_requester(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    sink = FakeSink()
    server = _make_server(loaded, principal_id=world["requester_id"], sink=sink)

    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="billing.delete_customer_data",
        arguments={},
        environment=world["production_id"],
    )
    operation_id = prepared["operation_id"]

    from n8n_operator.errors import OperationNotFoundError

    with (
        session_scope(loaded) as session,
        pytest.raises(OperationNotFoundError),
    ):
        service.approve_operation(
            session,
            operation_id=operation_id,
            decided_by=world["requester_id"],
            enable_v2=True,
        )

    status = await _call(server, "get_approval_status", operation_id=operation_id)
    assert world["requester_id"] not in status["approval_policy_snapshot"]
    assert status["ready"] is False
