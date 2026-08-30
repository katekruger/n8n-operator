"""Stage 06's own completion gate: the three named GTM usability proofs (BUILD_PLAN
section 12, ADR-005/ADR-009/ADR-012), driven through the real MCP tool layer where a
tool exists for the step — ``prepare_operation``/``retry_operation``/
``get_approval_status``/``get_operation`` are real MCP tool calls; the actual approval
decision and the reconciliation record both cross the CLI/``core.service`` boundary
directly (boundary B4: no MCP tool ever approves or asserts external evidence).

1. A failed enrichment (credential misconfiguration) safely retried after the
   credential is fixed — preflight now passes, so the retry actually runs instead of
   repeating the same failure.
2. A production CRM write, rejected once, retried — the retry needs its own fresh
   approval decision; it never inherits or reuses the rejected parent's (invariant
   I11).
3. An indeterminate campaign dispatch (``UNKNOWN``) reconciled by exact execution ID
   — an audit annotation only, the operation stays ``UNKNOWN`` throughout — before a
   human separately decides whether to call ``retry_operation`` on it. Reconciling
   never triggers a retry, and retrying never requires reconciling first.
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
    DispatchOutcome,
    ExecutionLookup,
    PreflightCheck,
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
  name: gtm-usability-stage06
workflows:
  - id: mkt.enrich_leads
    n8n_workflow_id: n8n-1
    title: Enrich inbound leads
    description: Read-only enrichment — auto-approved.
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
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: crm.bulk_update_stage
    n8n_workflow_id: n8n-2
    title: Bulk-update deal stage
    description: Production CRM write — requires approval.
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
  - id: mkt.launch_campaign
    n8n_workflow_id: n8n-3
    title: Launch a customer-facing campaign
    description: Auto-approved dispatch, correlation-tracked.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_c}
    risk: medium
    side_effects: read_only
    approval: none
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
""".format(hash_a="a" * 64, hash_b="b" * 64, hash_c="c" * 64)


class FakePreflight:
    """Configurable: simulates a credential misconfiguration until ``fixed`` is set,
    the same "GTM usability proof" shape ARCHITECTURE.md section 11 uses elsewhere."""

    def __init__(self, *, fixed: bool = True) -> None:
        self.fixed = fixed

    def check(self, workflow: Any) -> PreflightResult:
        if self.fixed:
            return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))
        return PreflightResult(
            ready=False,
            checks=[
                PreflightCheck(
                    check="credential_validity",
                    status="fail",
                    code="MISSING_NODE_CREDENTIALS",
                    detail="the enrichment node's API key is missing",
                )
            ],
            checked_at=datetime.now(UTC),
        )


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


class FakeReconciliation:
    def __init__(self, lookups: dict[str, ExecutionLookup]) -> None:
        self._lookups = lookups

    def get_execution(self, execution_id: str) -> ExecutionLookup:
        return self._lookups[execution_id]


def _make_server(
    session_factory: sessionmaker[Session], *, principal_id: str, preflight: FakePreflight
) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=preflight,
        health=FakeHealth(),
        dispatch=FakeDispatch(),
        definition=FakeDefinition(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        enable_v2=True,
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
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        production = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
            is_production=True,
        )
        admin = PrincipalRepository(session).create(kind="user", display_name="Admin")
        approver_a = PrincipalRepository(session).create(kind="user", display_name="Approver A")
        approver_b = PrincipalRepository(session).create(kind="user", display_name="Approver B")
        memberships = OrganizationMembershipRepository(session)
        memberships.create(principal_id=admin.id, organization_id=org.id, roles=["admin"])
        memberships.create(principal_id=approver_a.id, organization_id=org.id, roles=["approver"])
        memberships.create(principal_id=approver_b.id, organization_id=org.id, roles=["approver"])
        return {
            "env_id": production.id,
            "admin_id": admin.id,
            "approver_a": approver_a.id,
            "approver_b": approver_b.id,
        }


@pytest.mark.integration
async def test_failed_enrichment_safely_retried_after_a_credential_fix(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    broken_preflight = FakePreflight(fixed=False)
    server = _make_server(loaded, principal_id=world["admin_id"], preflight=broken_preflight)

    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="mkt.enrich_leads",
        arguments={},
        environment=world["env_id"],
    )
    # The broken credential is caught by preflight before approval is even
    # considered — BLOCKED (T03), not a failed execution.
    assert prepared["state"] == "BLOCKED"
    operation_id = prepared["operation_id"]

    # The credential is fixed; a fresh preflight now passes. The retry, driven
    # through the real MCP tool, must actually re-run preflight rather than
    # repeating the failure.
    fixed_preflight = FakePreflight(fixed=True)
    server = _make_server(loaded, principal_id=world["admin_id"], preflight=fixed_preflight)
    retried = await _call(server, "retry_operation", operation_id=operation_id)
    assert retried["state"] == "APPROVED"
    assert retried["parent_operation_id"] == operation_id


@pytest.mark.integration
async def test_production_crm_write_retried_after_rejection_needs_fresh_approval(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    server = _make_server(
        loaded, principal_id=world["admin_id"], preflight=FakePreflight(fixed=True)
    )
    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="crm.bulk_update_stage",
        arguments={},
        environment=world["env_id"],
    )
    assert prepared["state"] == "PENDING_APPROVAL"
    parent_id = prepared["operation_id"]

    with session_scope(loaded) as session:
        service.reject_operation(
            session, operation_id=parent_id, decided_by=world["approver_a"], enable_v2=True
        )

    retried = await _call(server, "retry_operation", operation_id=parent_id)
    assert retried["state"] == "PENDING_APPROVAL"
    child_id = retried["operation_id"]
    assert retried["parent_operation_id"] == parent_id

    # The child needs its own decision — approver_a's earlier rejection of the
    # *parent* never carries over.
    status = await _call(server, "get_approval_status", operation_id=child_id)
    assert status["decisions"] == []
    assert status["ready"] is False

    with session_scope(loaded) as session:
        approved = service.approve_operation(
            session, operation_id=child_id, decided_by=world["approver_a"], enable_v2=True
        )
    assert approved.state == "APPROVED"


@pytest.mark.integration
async def test_indeterminate_dispatch_reconciled_by_exact_id_before_a_human_decides_to_retry(
    loaded: sessionmaker[Session], world: dict[str, Any]
) -> None:
    server = _make_server(
        loaded, principal_id=world["admin_id"], preflight=FakePreflight(fixed=True)
    )
    prepared = await _call(
        server,
        "prepare_operation",
        workflow_id="mkt.launch_campaign",
        arguments={},
        environment=world["env_id"],
    )
    assert prepared["state"] == "APPROVED"
    operation_id = prepared["operation_id"]

    with session_scope(loaded) as session:
        service.execute_operation(
            session,
            operation_id=operation_id,
            handle=operation_id,
            principal_id=world["admin_id"],
            preflight=FakePreflight(fixed=True),
            enable_v2=True,
        )
        service.record_execution_outcome(
            session, operation_id=operation_id, outcome="indeterminate"
        )

    status_before = await _call(server, "get_operation", operation_id=operation_id)
    assert status_before["state"] == "UNKNOWN"

    # A human reconciles by exact execution ID (CLI-only — never an MCP tool). This
    # never touches the operation's own state.
    with session_scope(loaded) as session:
        record = service.reconcile_operation(
            session,
            operation_id=operation_id,
            principal_id=world["admin_id"],
            execution_id="exec-campaign-1",
            note="confirmed via n8n UI: campaign dispatched successfully",
            reconciliation=FakeReconciliation(
                {
                    "exec-campaign-1": ExecutionLookup(
                        execution_id="exec-campaign-1", n8n_workflow_id="n8n-3", status="success"
                    )
                }
            ),
            enable_v2=True,
        )
    assert record.n8n_execution_status == "success"

    status_after_reconciliation = await _call(server, "get_operation", operation_id=operation_id)
    assert status_after_reconciliation["state"] == "UNKNOWN"  # reconciling never resolves it

    # Only now, as a *separate* decision, does the human retry it.
    retried = await _call(server, "retry_operation", operation_id=operation_id)
    assert retried["state"] == "APPROVED"
    assert retried["parent_operation_id"] == operation_id

    # The original UNKNOWN operation is still exactly as it was.
    final_parent_status = await _call(server, "get_operation", operation_id=operation_id)
    assert final_parent_status["state"] == "UNKNOWN"
