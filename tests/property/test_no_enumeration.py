"""AC-39/AC-44: authorization can only narrow, never broaden, and a denial is
bitwise-identical to genuine absence (invariant I14 — no ``FORBIDDEN`` code, ever).

Two kinds of proof:

1. **Monotonicity** (pure, Hypothesis-driven, no database): removing a role, narrowing
   ``workflow_scope`` from ``*`` to a literal ID, or narrowing ``environment_scope``
   from ``["*"]`` to a subset can never turn a denial into an allow — "adding a
   restriction cannot increase access", tested as three independent properties (one per
   axis) rather than one combined one, so a failure names exactly which axis broke.
2. **AC-44's own scenario**: two real callers, one authorized for a workflow and one
   not, issuing four different tool calls against the same real operation ID and
   workflow ID — the unauthorized caller's response is bitwise identical in shape and
   error code to the same call against a nonexistent ID, for every one of the four
   tools MCP_TOOLS.md and AC-44 name (``describe_workflow``, ``get_operation``,
   ``list_operations``, and — standing in for ``diff_workflow_definition``/
   ``list_audit_events``, neither of which exists as an MCP tool until stage 04/08 —
   ``get_execution_result``, the fourth v1 tool ``_get_owned_operation_row`` gates the
   same way).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.authorization import ROLE_CAPABILITIES, Role, evaluate
from n8n_operator.core.models import HealthCheckResult, PreflightResult
from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.storage.models import OrganizationMembership
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

# ----------------------------------------------------------------------------------
# 1. Monotonicity — pure, no database.
# ----------------------------------------------------------------------------------

_ALL_ROLES: tuple[Role, ...] = ("viewer", "operator", "approver", "admin")
_ALL_TOOLS = sorted({tool for tools in ROLE_CAPABILITIES.values() for tool in tools})


def _membership(
    *,
    roles: list[str],
    workflow_scope: str = "*",
    environment_scope: list[str] | None = None,
    organization_id: str = "o1",
) -> OrganizationMembership:
    return OrganizationMembership(
        principal_id="p1",
        organization_id=organization_id,
        roles=roles,
        workflow_scope=workflow_scope,
        environment_scope=environment_scope if environment_scope is not None else ["*"],
    )


def _nonempty_subsets(roles: tuple[str, ...]) -> st.SearchStrategy[frozenset[str]]:
    return st.sets(st.sampled_from(roles), min_size=1).map(frozenset)


@given(
    wide_roles=_nonempty_subsets(_ALL_ROLES),
    tool_name=st.sampled_from(_ALL_TOOLS),
    data=st.data(),
)
def test_removing_a_role_never_increases_access(
    wide_roles: frozenset[str], tool_name: str, data: st.DataObject
) -> None:
    """M2's role set is a non-empty subset of M1's; both share identical
    workflow_scope/environment_scope (``*``/``["*"]``), isolating the role axis. If M2
    is allowed, M1 must be too — the converse need not hold."""
    narrow_roles = data.draw(_nonempty_subsets(tuple(wide_roles)))
    wide = _membership(roles=list(wide_roles))
    narrow = _membership(roles=list(narrow_roles))
    wide_decision = evaluate(memberships=[wide], tool_name=tool_name, workflow_id=None)
    narrow_decision = evaluate(memberships=[narrow], tool_name=tool_name, workflow_id=None)
    if narrow_decision.allowed:
        assert wide_decision.allowed, (
            f"narrower roles {sorted(narrow_roles)} allowed {tool_name!r} but wider "
            f"roles {sorted(wide_roles)} (a superset) did not"
        )


@given(
    role=st.sampled_from(_ALL_ROLES),
    tool_name=st.sampled_from(_ALL_TOOLS),
    workflow_id=st.text(min_size=1, max_size=20),
)
def test_narrowing_workflow_scope_from_wildcard_never_increases_access(
    role: Role, tool_name: str, workflow_id: str
) -> None:
    """M1 grants ``workflow_scope="*"``; M2 grants the literal ``workflow_id`` under
    test — a strict narrowing (unless ``workflow_id`` itself happens to be ``"*"``,
    excluded). If the narrow grant authorizes this exact workflow, the wildcard grant
    (which covers every workflow, including this one) must too."""
    if workflow_id == "*":
        return
    wide = _membership(roles=[role], workflow_scope="*")
    narrow = _membership(roles=[role], workflow_scope=workflow_id)
    wide_decision = evaluate(memberships=[wide], tool_name=tool_name, workflow_id=workflow_id)
    narrow_decision = evaluate(memberships=[narrow], tool_name=tool_name, workflow_id=workflow_id)
    if narrow_decision.allowed:
        assert wide_decision.allowed


@given(
    role=st.sampled_from(_ALL_ROLES),
    tool_name=st.sampled_from(_ALL_TOOLS),
    environment_id=st.text(min_size=1, max_size=20),
)
def test_narrowing_environment_scope_from_wildcard_never_increases_access(
    role: Role, tool_name: str, environment_id: str
) -> None:
    """As the workflow-scope property above, for ``environment_scope``: ``["*"]``
    (wide) vs. ``[environment_id]`` (narrow, a single named environment)."""
    if environment_id == "*":
        return
    wide = _membership(roles=[role], environment_scope=["*"])
    narrow = _membership(roles=[role], environment_scope=[environment_id])
    wide_decision = evaluate(
        memberships=[wide], tool_name=tool_name, workflow_id=None, environment_id=environment_id
    )
    narrow_decision = evaluate(
        memberships=[narrow], tool_name=tool_name, workflow_id=None, environment_id=environment_id
    )
    if narrow_decision.allowed:
        assert wide_decision.allowed


@given(role=st.sampled_from(_ALL_ROLES), tool_name=st.sampled_from(_ALL_TOOLS))
def test_a_wildcard_environment_scope_never_authorizes_another_organizations_environment(
    role: Role, tool_name: str
) -> None:
    """Stage 04's own closing of RR-13's "reachable but org-blind" gap: a membership's
    ``environment_scope: ["*"]`` means "every environment in *this membership's own*
    organization" (ADR-016 section 2), never "every environment ID anywhere" — a
    membership in org A must never authorize an environment that in fact belongs to
    org B, no matter how wide its own grant is."""
    membership = _membership(roles=[role], environment_scope=["*"], organization_id="org-a")
    decision = evaluate(
        memberships=[membership],
        tool_name=tool_name,
        workflow_id=None,
        environment_id="env-owned-by-org-b",
        environment_organization_id="org-b",
    )
    assert not decision.allowed


def test_an_empty_membership_list_is_never_more_permissive_than_any_membership() -> None:
    """The degenerate narrowing: zero memberships is the narrowest possible grant —
    strictly less permissive than any non-empty one, for every tool."""
    for tool_name in _ALL_TOOLS:
        assert not evaluate(memberships=[], tool_name=tool_name, workflow_id=None).allowed


# ----------------------------------------------------------------------------------
# 2. AC-44 — real callers, real database, bitwise-identical denial vs. absence.
# ----------------------------------------------------------------------------------


class _FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class _FakeHealth:
    def check(self) -> HealthCheckResult:
        return HealthCheckResult(reachable=True, checked_at=datetime.now(UTC))


class _FakeDispatch:
    def dispatch(self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int) -> Any:
        raise NotImplementedError

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class _FakeDefinition:
    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        raise NotImplementedError


_REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: ac44-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
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
"""


def _make_server(session_factory: sessionmaker[Session], *, principal_id: str) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=_FakePreflight(),
        health=_FakeHealth(),
        dispatch=_FakeDispatch(),
        definition=_FakeDefinition(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        approval_base_url="http://127.0.0.1:8765",
        enable_v2=True,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))
    register_resources(server, deps)
    return server


async def _call(server: MCPServer[Any], name: str, **arguments: Any) -> CallToolResult:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    return result


@pytest.mark.integration
async def test_unauthorized_and_nonexistent_are_bitwise_identical_across_four_tools(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(_REGISTRY_YAML.format(hash="a" * 64))

    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        EnvironmentRepository(session).create(
            organization_id=org.id,
            name="default",
            n8n_base_url_ref="env:N8N_TEST_BASE_URL",
            n8n_api_key_ref="env:N8N_TEST_API_KEY",
        )
        authorized = PrincipalRepository(session).create(kind="user", display_name="Alice")
        unauthorized = PrincipalRepository(session).create(kind="user", display_name="Bob")
        OrganizationMembershipRepository(session).create(
            principal_id=authorized.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="crm.*",
        )
        OrganizationMembershipRepository(session).create(
            principal_id=unauthorized.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="billing.*",  # deliberately does not match crm.sync_contact
        )
        authorized_id, unauthorized_id = authorized.id, unauthorized.id

    authorized_server = _make_server(session_factory, principal_id=authorized_id)
    unauthorized_server = _make_server(session_factory, principal_id=unauthorized_id)

    prepare_result = await _call(
        authorized_server, "prepare_operation", workflow_id="crm.sync_contact", arguments={}
    )
    assert not prepare_result.is_error
    operation_id = json.loads(prepare_result.content[0].text)["operation_id"]  # type: ignore[union-attr]

    real_workflow_id = "crm.sync_contact"
    fake_workflow_id = "crm.sync_contact_does_not_exist"
    real_operation_id = operation_id
    fake_operation_id = "op_does_not_exist_00000000000000000"

    for tool_name, arg_name, real_value, fake_value in (
        ("describe_workflow", "workflow_id", real_workflow_id, fake_workflow_id),
        ("get_operation", "operation_id", real_operation_id, fake_operation_id),
        ("get_execution_result", "operation_id", real_operation_id, fake_operation_id),
    ):
        unauthorized_result = await _call(unauthorized_server, tool_name, **{arg_name: real_value})
        nonexistent_result = await _call(authorized_server, tool_name, **{arg_name: fake_value})
        assert unauthorized_result.is_error == nonexistent_result.is_error
        unauthorized_body = json.loads(unauthorized_result.content[0].text)  # type: ignore[union-attr]
        nonexistent_body = json.loads(nonexistent_result.content[0].text)  # type: ignore[union-attr]
        assert unauthorized_body.keys() == nonexistent_body.keys() == {"error"}
        assert unauthorized_body["error"]["code"] == nonexistent_body["error"]["code"], tool_name
        assert "code" in unauthorized_body["error"]
        assert unauthorized_body["error"]["code"] != "FORBIDDEN"

    # list_operations: filtering, not a single not-found — the unauthorized caller's
    # scope (billing.*) simply never includes this operation's workflow (crm.*), so it
    # never appears, the same as it would for a genuinely nonexistent one.
    listed = await _call(unauthorized_server, "list_operations")
    listed_body = json.loads(listed.content[0].text)  # type: ignore[union-attr]
    assert real_operation_id not in {op["operation_id"] for op in listed_body["operations"]}
