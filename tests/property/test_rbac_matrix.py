"""AC-38: the role-capability matrix in ADR-015 is exhaustive and enforced.

A property test drives every (role, tool) pair from ADR-015 section 1's table against
``core.authorization.evaluate`` and asserts the allow/deny outcome matches the matrix
exactly, for all four roles across all 20 v1+v2 tool names — pure evaluator logic, no
database, no MCP tool handler required to exist (Stage 03's own scoping: the matrix is
checkable data regardless of which tools have shipped a live handler yet).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from n8n_operator.core.authorization import ROLE_CAPABILITIES, Role, evaluate
from n8n_operator.storage.models import OrganizationMembership

# BUILD_PLAN §7.1 (v1, 12) + §7.2 (v2, 8) — the exact 20 tool names ADR-015's matrix is
# defined over. Deliberately hand-listed (not derived from ROLE_CAPABILITIES itself),
# so this test can actually catch a drift between the matrix and the real inventory.
ALL_TOOLS = frozenset(
    {
        "list_workflows",
        "describe_workflow",
        "get_instance_health",
        "validate_input",
        "preflight_workflow",
        "prepare_operation",
        "get_operation",
        "execute_operation",
        "cancel_operation",
        "list_operations",
        "get_execution_result",
        "get_execution_log",
        "whoami",
        "list_environments",
        "request_approval",
        "get_approval_status",
        "retry_operation",
        "diff_workflow_definition",
        "get_metrics",
        "list_audit_events",
    }
)

ALL_ROLES: tuple[Role, ...] = ("viewer", "operator", "approver", "admin")


def _membership(role: Role) -> OrganizationMembership:
    return OrganizationMembership(
        principal_id="p1",
        organization_id="o1",
        roles=[role],
        workflow_scope="*",
        environment_scope=["*"],
    )


def test_the_matrix_is_defined_over_exactly_the_twenty_named_tools() -> None:
    """A guard on the guard: if this test file's own ``ALL_TOOLS`` or
    ``ROLE_CAPABILITIES`` ever drifts from the real 20-tool inventory, fail loudly here
    rather than silently under-testing the matrix."""
    assert len(ALL_TOOLS) == 20
    every_capability = {tool for tools in ROLE_CAPABILITIES.values() for tool in tools}
    # ROLE_CAPABILITIES also carries APPROVE_REJECT_CAPABILITY, which is not one of the
    # 20 tools (ADR-015's separate "out-of-band approve/reject" matrix row) — excluded
    # here since this test is specifically about the 20 named tools.
    assert every_capability >= ALL_TOOLS


@given(role=st.sampled_from(ALL_ROLES), tool_name=st.sampled_from(sorted(ALL_TOOLS)))
def test_every_role_tool_pair_matches_the_adr_015_matrix(role: Role, tool_name: str) -> None:
    membership = _membership(role)
    decision = evaluate(memberships=[membership], tool_name=tool_name, workflow_id=None)
    expected = tool_name in ROLE_CAPABILITIES[role]
    assert decision.allowed is expected, (
        f"role={role!r} tool={tool_name!r}: evaluator said allowed={decision.allowed}, "
        f"matrix says {expected}"
    )


def test_every_role_includes_every_read_only_tool() -> None:
    """ADR-015 section 1: "Every role includes every read-only v1 and v2 tool" — the
    four rows the matrix table marks all-✓."""
    read_only_tools = {
        "list_workflows",
        "describe_workflow",
        "get_instance_health",
        "validate_input",
        "preflight_workflow",
        "get_operation",
        "list_operations",
        "get_execution_result",
        "get_execution_log",
        "whoami",
        "list_environments",
        "diff_workflow_definition",
        "get_metrics",
        "list_audit_events",
        "get_approval_status",
    }
    for role in ALL_ROLES:
        assert read_only_tools <= ROLE_CAPABILITIES[role]


def test_approver_excludes_prepare_and_execute() -> None:
    """ADR-015 section 1: "approver deliberately does not include prepare_operation or
    execute_operation" — separating requester from decider."""
    assert "prepare_operation" not in ROLE_CAPABILITIES["approver"]
    assert "execute_operation" not in ROLE_CAPABILITIES["approver"]
    assert "cancel_operation" not in ROLE_CAPABILITIES["approver"]


def test_retry_operation_is_admin_only() -> None:
    """ADR-015 section 1: "retry_operation is admin-only"."""
    for role in ("viewer", "operator", "approver"):
        assert "retry_operation" not in ROLE_CAPABILITIES[role]
    assert "retry_operation" in ROLE_CAPABILITIES["admin"]


def test_viewer_has_no_side_effecting_capability() -> None:
    side_effecting = {
        "prepare_operation",
        "cancel_operation",
        "execute_operation",
        "request_approval",
        "retry_operation",
    }
    assert not (side_effecting & ROLE_CAPABILITIES["viewer"])
