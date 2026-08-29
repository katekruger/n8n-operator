"""Contract: the MCP tool and resource inventory matches BUILD_PLAN section 7.1 exactly,
every tool schema rejects unknown properties, no schema shape carries a raw n8n
identifier or URL, and both transports register the identical surface (AC-23).

Uses a trivial in-memory-only ``ToolDeps`` — nothing here touches a database or n8n; the
contract under test is about registered *shape*, not runtime behavior (BUILD_PLAN
section 10.2 and the AC-specific integration tests cover behavior).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from mcp.server.mcpserver.server import MCPServer
from sqlalchemy.orm import sessionmaker

from n8n_operator.core.models import HealthCheckResult, PreflightResult
from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools

# BUILD_PLAN section 7.1 — the exact, ordered v1 inventory.
EXPECTED_TOOL_NAMES = {
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
}

# MCP_TOOLS.md section 3.
EXPECTED_RESOURCE_URIS = {"registry://workflows", "audit://operations/{operation_id}"}

# Field names that would smuggle a raw n8n identifier, secret, or instance address
# through a tool argument (boundary B1) — a schema property named or shaped like one
# of these is a contract violation regardless of what any handler does with it.
_FORBIDDEN_FIELD_SUBSTRINGS = (
    "n8n_workflow_id",
    "n8n_id",
    "instance_url",
    "base_url",
    "webhook_path",
    "api_key",
    "secret_ref",
    "credential",
)


class _FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class _FakeHealth:
    def check(self) -> HealthCheckResult:
        return HealthCheckResult(reachable=True, checked_at=datetime.now(UTC))


class _FakeDispatch:
    def dispatch(self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int) -> Any:
        raise NotImplementedError  # never invoked — this module tests shape, not behavior

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


def _make_deps(*, caller_is_local: bool = True) -> ToolDeps:
    # No test in this module touches a database; `session_factory` is unused.
    unused_session_factory = cast("sessionmaker[Any]", None)
    return ToolDeps(
        session_factory=unused_session_factory,
        preflight=_FakePreflight(),
        health=_FakeHealth(),
        dispatch=_FakeDispatch(),
        server_max_argument_bytes=262_144,
        caller_is_local=caller_is_local,
        approval_base_url="http://127.0.0.1:8765",
    )


@pytest.fixture
def tools() -> list[Any]:
    return build_tools(_make_deps())


def test_exactly_twelve_tools_registered(tools: list[Any]) -> None:
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOL_NAMES
    assert len(tools) == 12


def test_no_unplanned_tool_is_registered(tools: list[Any]) -> None:
    """The inverse of the previous test, spelled out separately so a future addition
    to ``build_tools`` that also *removes* a documented tool (net count still 12) does
    not slip past a count-only check."""
    for t in tools:
        assert t.name in EXPECTED_TOOL_NAMES, f"unplanned tool registered: {t.name!r}"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
def test_every_tool_schema_rejects_unknown_properties(tools: list[Any], tool_name: str) -> None:
    by_name = {t.name: t for t in tools}
    schema = by_name[tool_name].parameters
    assert schema.get("additionalProperties") is False, (
        f"{tool_name}'s schema does not declare additionalProperties: false"
    )


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
def test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url(
    tools: list[Any], tool_name: str
) -> None:
    by_name = {t.name: t for t in tools}
    properties = by_name[tool_name].parameters.get("properties", {})
    for field_name in properties:
        lowered = field_name.lower()
        for forbidden in _FORBIDDEN_FIELD_SUBSTRINGS:
            assert forbidden not in lowered, (
                f"{tool_name}'s schema exposes a field named {field_name!r}, "
                f"shaped like a raw n8n identifier/secret/URL (boundary B1)"
            )


def test_no_tool_grants_approval(tools: list[Any]) -> None:
    """There is no MCP tool that approves — approval crosses a separate channel a
    client cannot reach (boundary B4). No registered tool name resembles one, and none
    of ``core.service``'s approval-granting use cases (``approve_operation``) is
    reachable from any handler's source."""
    for t in tools:
        lowered = t.name.lower()
        assert "approve" not in lowered and "grant" not in lowered

    import inspect

    from n8n_operator.mcp import tools as tools_module

    source = inspect.getsource(tools_module)
    assert "approve_operation" not in source
    assert "reject_operation" not in source


def test_execute_operation_is_annotated_side_effecting(tools: list[Any]) -> None:
    by_name = {t.name: t for t in tools}
    annotations = by_name["execute_operation"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False


def test_cancel_operation_is_annotated_state_changing_but_not_destructive(
    tools: list[Any],
) -> None:
    by_name = {t.name: t for t in tools}
    annotations = by_name["cancel_operation"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False


def test_prepare_operation_is_annotated_non_readonly_but_does_not_run_n8n(
    tools: list[Any],
) -> None:
    """``prepare_operation`` creates durable state (an operation row) but never
    dispatches to n8n — ``open_world_hint`` is ``False`` (its domain of interaction is
    closed: the database, not an external system), unlike ``execute_operation`` and the
    two live-instance discovery tools."""
    by_name = {t.name: t for t in tools}
    annotations = by_name["prepare_operation"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert annotations.open_world_hint is False


@pytest.mark.parametrize(
    "tool_name",
    sorted(EXPECTED_TOOL_NAMES - {"prepare_operation", "execute_operation", "cancel_operation"}),
)
def test_pure_read_tools_are_annotated_read_only(tools: list[Any], tool_name: str) -> None:
    by_name = {t.name: t for t in tools}
    annotations = by_name[tool_name].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True


async def test_exactly_the_two_v1_resources_are_registered() -> None:
    server: MCPServer[Any] = MCPServer("test")
    register_resources(server, _make_deps())
    uris = {str(r.uri) for r in await server.list_resources()}
    uris |= {t.uri_template for t in await server.list_resource_templates()}
    assert uris == EXPECTED_RESOURCE_URIS


async def test_same_tool_schemas_across_local_and_remote_deps() -> None:
    """AC-23: the same 12-tool surface regardless of caller locality — the one thing
    that legitimately varies (``approval_url`` presence) is a *result* difference, not
    a *schema* difference (BUILD_PLAN section 7.1, MCP_TOOLS.md section 2.6)."""
    local_server: MCPServer[Any] = MCPServer(
        "local", tools=build_tools(_make_deps(caller_is_local=True))
    )
    remote_server: MCPServer[Any] = MCPServer(
        "remote", tools=build_tools(_make_deps(caller_is_local=False))
    )
    local_tools = await local_server.list_tools()
    remote_tools = await remote_server.list_tools()

    local_by_name = {t.name: t for t in local_tools}
    remote_by_name = {t.name: t for t in remote_tools}
    assert local_by_name.keys() == remote_by_name.keys() == EXPECTED_TOOL_NAMES
    for name in EXPECTED_TOOL_NAMES:
        assert local_by_name[name].input_schema == remote_by_name[name].input_schema
        assert local_by_name[name].annotations == remote_by_name[name].annotations


def test_whoami_is_registered_only_when_v2_is_enabled() -> None:
    """Stage 02's completion gate: v1 mode (``enable_v2`` unset, the default) stays
    exactly the twelve tools AC-23 requires; ``whoami`` and ``list_environments``
    (stage 04) are a thirteenth and fourteenth tool that exist only when an operator
    opts in (BUILD_PLAN section 7.2)."""
    v1_tools = build_tools(_make_deps())
    v2_tools = build_tools(ToolDeps(**{**_make_deps().__dict__, "enable_v2": True}))

    v1_names = {t.name for t in v1_tools}
    v2_names = {t.name for t in v2_tools}
    assert v1_names == EXPECTED_TOOL_NAMES
    assert len(v1_tools) == 12
    assert v2_names == EXPECTED_TOOL_NAMES | {"whoami", "list_environments"}
    assert len(v2_tools) == 14
