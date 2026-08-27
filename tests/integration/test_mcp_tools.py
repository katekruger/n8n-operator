"""The twelve v1 MCP tools against a real SQLite database (BUILD_PLAN section 12, phase 5).

Drives full ``MCPServer.call_tool`` round trips (schema validation, dispatch, JSON
content conversion) rather than calling handler functions directly, so a regression in
the manual ``Tool`` construction (``mcp/tools.py``) would show up here exactly as it
would to a real client. Uses a fake ``PreflightPort``/``HealthPort`` throughout — no
network, no real n8n — the same seam ``tests/integration/test_core_service_operations.py``
uses for the same reason.

Covers AC-01, AC-03, AC-04, AC-31, secret/result shaping, and the argument-size limit
enforced identically through this transport (B12).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult, InputRequiredResult
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import (
    DispatchOutcome,
    HealthCheckResult,
    PreflightCheck,
    PreflightResult,
)
from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase5-test
workflows:
  - id: wf.approval
    n8n_workflow_id: n8n-1
    title: Needs approval
    description: Writes to an external system.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    tags: [crm]
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties:
        email: {{type: string}}
      required: [email]
      additionalProperties: false
    output:
      include_node_trace: true
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: wf.auto
    n8n_workflow_id: n8n-2
    title: Auto approved
    description: Read-only reporting.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      properties:
        token: {{type: string}}
      additionalProperties: false
    output:
      redact: ["$.token"]
      max_bytes: 65536
    limits:
      execution_ttl_seconds: 300
  - id: wf.disabled
    n8n_workflow_id: n8n-3
    title: Disabled
    description: Retired.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_c}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/c
      auth: none
    input_schema:
      type: object
      additionalProperties: false
    enabled: false
""".format(hash_a="a" * 64, hash_b="b" * 64, hash_c="c" * 64)


class FakePreflight:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def check(self, workflow: Any) -> PreflightResult:
        status = "pass" if self.ready else "fail"
        return PreflightResult(
            ready=self.ready,
            checks=[
                PreflightCheck(
                    check="workflow_active",
                    status=status,  # type: ignore[arg-type]
                    code=None if self.ready else "WORKFLOW_INACTIVE",
                )
            ],
            checked_at=datetime.now(UTC),
        )


class FakeHealth:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable

    def check(self) -> HealthCheckResult:
        if not self.reachable:
            return HealthCheckResult(
                reachable=False, reason="INSTANCE_UNREACHABLE", checked_at=datetime.now(UTC)
            )
        return HealthCheckResult(
            reachable=True, n8n_version="1.1.1", latency_ms=7, checked_at=datetime.now(UTC)
        )


class FakeDispatch:
    """A configurable stand-in for the real (phase 7) n8n dispatch adapter — never
    actually invoked by tests that raise before ``core.service.dispatch_operation``,
    and unused by tests that simulate a completed operation via
    ``service.record_execution_outcome`` directly rather than through this tool."""

    def __init__(
        self,
        *,
        outcome: DispatchOutcome | None = None,
        node_trace: dict[str, Any] | None = None,
    ) -> None:
        self.outcome = outcome or DispatchOutcome(
            kind="success",
            http_status=200,
            result={},
            execution_id=None,
            correlation_available=False,
        )
        self.node_trace = node_trace
        self.dispatch_calls = 0
        self.fetch_node_trace_calls = 0

    def dispatch(
        self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        self.dispatch_calls += 1
        return self.outcome

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        self.fetch_node_trace_calls += 1
        return self.node_trace


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], registry_path: Path) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


def make_server(
    session_factory: sessionmaker[Session],
    *,
    caller_is_local: bool = True,
    preflight: FakePreflight | None = None,
    health: FakeHealth | None = None,
    dispatch: FakeDispatch | None = None,
    server_max_argument_bytes: int = 262_144,
) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=preflight or FakePreflight(),
        health=health or FakeHealth(),
        dispatch=dispatch or FakeDispatch(),
        server_max_argument_bytes=server_max_argument_bytes,
        caller_is_local=caller_is_local,
        approval_base_url="http://127.0.0.1:8765",
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))
    register_resources(server, deps)
    return server


async def call(server: MCPServer[Any], name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)  # none of these 12 tools uses elicitation
    assert not result.is_error, f"{name} unexpectedly errored: {result.content}"
    assert len(result.content) == 1
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------
# AC-01 — a workflow absent from the registry is invisible and unpreparable, with no
# signal distinguishing "unregistered" from "nonexistent".
# --------------------------------------------------------------------------------------


async def test_ac01_unregistered_workflow_absent_from_list(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(server, "list_workflows")
    ids = {w["workflow_id"] for w in result["workflows"]}
    assert "wf.nonexistent" not in ids
    assert "wf.disabled" not in ids  # disabled is equally invisible to discovery


async def test_ac01_prepare_on_unregistered_workflow_returns_workflow_not_found(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(server, "prepare_operation", workflow_id="wf.nonexistent", arguments={})
    assert result["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_ac01_disabled_workflow_is_distinct_from_unregistered(
    loaded: sessionmaker[Session],
) -> None:
    """A workflow present in the registry but ``enabled: false`` is a *different*
    failure (``WORKFLOW_DISABLED``) from one absent entirely (``WORKFLOW_NOT_FOUND``)
    at ``prepare_operation`` — unlike discovery, which (the previous test) treats both
    as equally invisible. MCP_TOOLS.md section 2.6 documents this distinction
    explicitly; it is not the "no signal" guarantee AC-01 makes about *unregistered vs.
    registered-but-absent-from-n8n*, which is a different pair of cases."""
    server = make_server(loaded)
    disabled = await call(server, "prepare_operation", workflow_id="wf.disabled", arguments={})
    nonexistent = await call(
        server, "prepare_operation", workflow_id="wf.nonexistent", arguments={}
    )
    assert disabled["error"]["code"] == "WORKFLOW_DISABLED"
    assert nonexistent["error"]["code"] == "WORKFLOW_NOT_FOUND"


# --------------------------------------------------------------------------------------
# AC-03 — describe_workflow's shape and its absence of any n8n ID, URL, or secret ref.
# --------------------------------------------------------------------------------------


async def test_ac03_describe_workflow_returns_the_documented_contract(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(server, "describe_workflow", workflow_id="wf.approval")
    assert result["input_schema"]["required"] == ["email"]
    assert result["approval"] == "required"
    assert result["risk"] == "medium"
    assert result["side_effects"] == "external_write"
    assert result["limits"]["approval_ttl_seconds"] == 900
    assert result["limits"]["execution_ttl_seconds"] == 300


async def test_ac03_describe_workflow_leaks_no_n8n_id_url_or_secret(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(server, "describe_workflow", workflow_id="wf.approval")
    serialized = json.dumps(result)
    for forbidden in ("n8n-1", "n8n_workflow_id", "secret_ref", "webhook", "127.0.0.1"):
        assert forbidden not in serialized, f"describe_workflow leaked {forbidden!r}"


async def test_ac03_describe_workflow_unknown_id_is_workflow_not_found(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(server, "describe_workflow", workflow_id="wf.nonexistent")
    assert result["error"]["code"] == "WORKFLOW_NOT_FOUND"


# --------------------------------------------------------------------------------------
# AC-04 — validate_input's structured, path-anchored errors.
# --------------------------------------------------------------------------------------


async def test_ac04_missing_required_field(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(server, "validate_input", workflow_id="wf.approval", arguments={})
    assert result["valid"] is False
    assert any(e["path"] == "/email" and e["code"] == "REQUIRED" for e in result["errors"])


async def test_ac04_wrong_typed_field(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(
        server, "validate_input", workflow_id="wf.approval", arguments={"email": 42}
    )
    assert result["valid"] is False
    assert any(e["path"] == "/email" for e in result["errors"])


async def test_ac04_unknown_extra_field(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(
        server,
        "validate_input",
        workflow_id="wf.approval",
        arguments={"email": "a@b.com", "nickname": "x"},
    )
    assert result["valid"] is False
    assert any(
        e["path"] == "/nickname" and e["code"] == "ADDITIONAL_PROPERTY" for e in result["errors"]
    )


async def test_ac04_valid_arguments_pass(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(
        server, "validate_input", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    assert result == {"valid": True, "errors": []}


# --------------------------------------------------------------------------------------
# AC-31 — approval_url is present for a local caller only.
# --------------------------------------------------------------------------------------


async def test_ac31_approval_url_present_for_local_caller(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, caller_is_local=True)
    result = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    assert result["state"] == "PENDING_APPROVAL"
    assert result["approval_required"] is True
    assert "approval_url" in result
    assert result["approval_url"].startswith("http://127.0.0.1:8765/approve/")


async def test_ac31_no_approval_url_for_remote_caller(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, caller_is_local=False)
    result = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    assert result["state"] == "PENDING_APPROVAL"
    assert result["approval_required"] is True
    assert "approval_url" not in result
    serialized = json.dumps(result)
    assert "127.0.0.1" not in serialized and "approve/" not in serialized


async def test_ac31_auto_approved_workflow_never_needs_a_url(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, caller_is_local=True)
    result = await call(server, "prepare_operation", workflow_id="wf.auto", arguments={})
    assert result["state"] == "APPROVED"
    assert result["approval_required"] is False
    assert "approval_url" not in result


async def test_prepare_operation_invalid_shape_carries_json_pointer_errors(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={}
    )  # missing required "email"
    assert result["state"] == "INVALID"
    assert result["workflow_id"] == "wf.approval"
    assert any(e["path"] == "/email" and e["code"] == "REQUIRED" for e in result["errors"])
    assert "approval_required" not in result


async def test_prepare_operation_blocked_shape_carries_preflight_checks(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, preflight=FakePreflight(ready=False))
    result = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    assert result["state"] == "BLOCKED"
    assert result["workflow_id"] == "wf.approval"
    assert any(c["status"] == "fail" for c in result["checks"])
    assert "approval_required" not in result


async def test_list_operations_rejects_a_malformed_since_timestamp(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    result = await call(server, "list_operations", since="not-a-timestamp")
    assert result["error"]["code"] == "INVALID_ARGUMENTS"


# --------------------------------------------------------------------------------------
# Secret / result shaping (boundary B5) — output.redact honored, no configured secret
# leaks through describe_workflow, get_operation's echoed arguments, or errors.
# --------------------------------------------------------------------------------------


async def test_redacted_argument_is_echoed_as_redacted_in_get_operation(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, caller_is_local=True)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.auto", arguments={"token": "shh-secret"}
    )
    op_id = prepared["operation_id"]
    result = await call(server, "get_operation", operation_id=op_id)
    assert result["arguments"]["token"] == "[REDACTED]"
    assert "shh-secret" not in json.dumps(result)


async def test_no_tool_result_ever_contains_the_configured_n8n_api_key_or_url(
    loaded: sessionmaker[Session],
) -> None:
    """A cheap, direct analogue of AC-18's Hypothesis property, scoped to what this
    module's own fixtures can exercise without a network in the loop."""
    server = make_server(loaded, caller_is_local=True)
    calls: list[dict[str, Any]] = [
        await call(server, "list_workflows"),
        await call(server, "describe_workflow", workflow_id="wf.approval"),
        await call(server, "get_instance_health"),
        await call(server, "preflight_workflow", workflow_id="wf.approval"),
    ]
    blob = json.dumps(calls)
    for secret in ("sk-", "Bearer ", "X-N8N-API-KEY", "n8n-workflow-1"):
        assert secret not in blob


# --------------------------------------------------------------------------------------
# Argument-size limit (B12) enforced identically through this transport.
# --------------------------------------------------------------------------------------


async def test_oversized_arguments_are_refused_and_create_no_operation(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, server_max_argument_bytes=64)
    before = await call(server, "list_operations")
    result = await call(
        server,
        "prepare_operation",
        workflow_id="wf.approval",
        arguments={"email": "a@b.com", "junk": "x" * 500},
    )
    assert result["error"]["code"] == "ARGUMENTS_TOO_LARGE"
    after = await call(server, "list_operations")
    assert len(after["operations"]) == len(before["operations"])


# --------------------------------------------------------------------------------------
# Agent cannot approve: no tool transitions PENDING_APPROVAL -> APPROVED, and the only
# tool that could — execute_operation — refuses instead (boundary B4).
# --------------------------------------------------------------------------------------


async def test_execute_operation_refuses_a_pending_approval_operation(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, caller_is_local=True)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]
    result = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert result["error"]["code"] == "APPROVAL_REQUIRED"

    # Still PENDING_APPROVAL — the refused attempt changed nothing.
    state = await call(server, "get_operation", operation_id=op_id)
    assert state["state"] == "PENDING_APPROVAL"


async def test_calling_an_unknown_tool_name_fails(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    for bogus_name in ("approve_operation", "approve", "grant_approval"):
        with pytest.raises(Exception):  # noqa: B017 - the SDK's own ToolError/unknown-tool path
            await server.call_tool(bogus_name, {"operation_id": "op_x"})


# --------------------------------------------------------------------------------------
# get_instance_health, cancel_operation, list_operations, get_execution_result,
# get_execution_log, preflight_workflow — one straightforward shape check each.
# --------------------------------------------------------------------------------------


async def test_get_instance_health_reachable(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, health=FakeHealth(reachable=True))
    result = await call(server, "get_instance_health")
    assert result["reachable"] is True
    assert result["n8n_version"] == "1.1.1"


async def test_get_instance_health_unreachable_is_a_result_not_an_error(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, health=FakeHealth(reachable=False))
    result = await server.call_tool("get_instance_health", {})
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert payload == {
        "reachable": False,
        "reason": "INSTANCE_UNREACHABLE",
        "checked_at": payload["checked_at"],
    }


async def test_preflight_workflow_blocked(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, preflight=FakePreflight(ready=False))
    result = await call(server, "preflight_workflow", workflow_id="wf.approval")
    assert result["ready"] is False
    assert any(c["status"] == "fail" for c in result["checks"])


async def test_cancel_operation(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]
    result = await call(server, "cancel_operation", operation_id=op_id, reason="no longer needed")
    assert result["state"] == "CANCELED"


async def test_list_operations_reports_a_next_cursor_only_on_a_full_page(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    for _ in range(3):
        await call(
            server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
        )
    full_page = await call(server, "list_operations", limit=2)
    assert len(full_page["operations"]) == 2
    assert full_page["next_cursor"] is not None

    short_page = await call(server, "list_operations", limit=100)
    assert len(short_page["operations"]) == 3
    assert short_page["next_cursor"] is None


async def test_get_execution_result_before_execution_is_result_not_available(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    result = await call(server, "get_execution_result", operation_id=prepared["operation_id"])
    assert result["error"]["code"] == "RESULT_NOT_AVAILABLE"


async def test_get_execution_log_before_execution_is_result_not_available(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    result = await call(server, "get_execution_log", operation_id=prepared["operation_id"])
    assert result["error"]["code"] == "RESULT_NOT_AVAILABLE"


async def test_get_operation_unknown_id(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    result = await call(server, "get_operation", operation_id="op_does_not_exist")
    assert result["error"]["code"] == "OPERATION_NOT_FOUND"


# --------------------------------------------------------------------------------------
# The two v1 resources (MCP_TOOLS.md section 3).
# --------------------------------------------------------------------------------------


async def read_resource_json(server: MCPServer[Any], uri: str) -> dict[str, Any]:
    contents = await server.read_resource(uri)
    assert not isinstance(contents, InputRequiredResult)
    (item,) = list(contents)
    assert isinstance(item.content, str)
    return json.loads(item.content)  # type: ignore[no-any-return]


async def test_registry_workflows_resource_excludes_n8n_id_and_secrets(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    payload = await read_resource_json(server, "registry://workflows")
    ids = {w["workflow_id"] for w in payload["workflows"]}
    assert ids == {"wf.approval", "wf.auto"}  # wf.disabled excluded, same as list_workflows
    serialized = json.dumps(payload)
    for forbidden in ("n8n-1", "n8n_workflow_id", "trigger", "secret_ref"):
        assert forbidden not in serialized


async def test_audit_operation_resource_returns_the_event_chain(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]

    payload = await read_resource_json(server, f"audit://operations/{op_id}")
    assert payload["operation_id"] == op_id
    transitions = [e["transition"] for e in payload["events"]]
    assert transitions == ["T01", "T04"]  # PREPARING, then PENDING_APPROVAL


async def test_audit_operation_resource_unknown_id_raises_resource_not_found(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded)
    with pytest.raises(ResourceNotFoundError):
        await server.read_resource("audit://operations/op_does_not_exist")


# --------------------------------------------------------------------------------------
# get_execution_result / get_execution_log's success shapes, once an operation has
# actually completed. Nothing in Phase 5 dispatches to n8n yet (BUILD_PLAN section 12,
# Phase 7) — ``core.service.record_execution_outcome`` is the seam Phase 4 already
# built for exactly this ("the seam Phase 4's n8n adapter calls after dispatch"), used
# directly here to simulate a completed EXECUTING operation without a network in the
# loop, the same way its own test suite does.
# --------------------------------------------------------------------------------------


async def _complete_an_operation(
    loaded: sessionmaker[Session], server: MCPServer[Any], *, outcome: str
) -> str:
    """Prepares, approves, and burns the handle via ``core.service`` directly — not
    through the ``execute_operation`` *tool*, which (phase 7) also dispatches and fully
    resolves the operation in one call. Bypassing the tool for the burn step keeps this
    helper's original design intact: simulate a completed ``EXECUTING`` operation via
    ``service.record_execution_outcome`` with no network, and no ``FakeDispatch``
    outcome to keep in sync with the ``outcome`` parameter below."""
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id: str = prepared["operation_id"]
    with session_scope(loaded) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    with session_scope(loaded) as session:
        if outcome == "success":
            service.record_execution_outcome(
                session, operation_id=op_id, outcome="success", result={"contact_id": "c_1"}
            )
        else:
            service.record_execution_outcome(
                session,
                operation_id=op_id,
                outcome="error",
                error={"node": "HTTP Request", "message": "boom"},
            )
    return op_id


async def test_get_execution_result_success_shape(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    op_id = await _complete_an_operation(loaded, server, outcome="success")
    result = await call(server, "get_execution_result", operation_id=op_id)
    assert result["state"] == "SUCCEEDED"
    assert result["status"] == "success"
    assert result["result"] == {"contact_id": "c_1"}
    assert result["truncated"] is False


async def test_get_execution_result_error_shape(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    op_id = await _complete_an_operation(loaded, server, outcome="error")
    result = await call(server, "get_execution_result", operation_id=op_id)
    assert result["state"] == "FAILED"
    assert result["error"] == {"node": "HTTP Request", "message": "boom"}


async def test_get_execution_log_after_completion(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded)
    op_id = await _complete_an_operation(loaded, server, outcome="success")
    result = await call(server, "get_execution_log", operation_id=op_id)
    assert result["state"] == "SUCCEEDED"
    # No node trace was ever recorded — this helper simulates completion via
    # service.record_execution_outcome directly, with no node_trace argument, rather
    # than through a real (or fake) dispatch. See the tests below for a real dispatch.
    assert result["nodes"] == []
    assert result["truncated"] is False


# --------------------------------------------------------------------------------------
# Phase 7: the execute_operation tool's own dispatch orchestration and result shapes
# (MCP_TOOLS.md 2.8), and get_execution_log naming a failing node (AC-15) — both
# through a real (fake) DispatchPort, unlike _complete_an_operation above.
# --------------------------------------------------------------------------------------


async def test_execute_operation_tool_succeeded_shape(loaded: sessionmaker[Session]) -> None:
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="success",
            http_status=200,
            result={"contact_id": "c_1", "created": False},
            execution_id="exec-1",
            correlation_available=True,
        )
    )
    server = make_server(loaded, dispatch=dispatch)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]
    with session_scope(loaded) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")

    result = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert result["operation_id"] == op_id
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["contact_id"] == "c_1"
    assert result["result"]["truncated"] is False
    assert result["started_at"] is not None
    assert result["finished_at"] is not None
    assert result["duration_ms"] is not None
    assert dispatch.dispatch_calls == 1

    # A reused handle dispatches nothing further.
    second = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert second["error"]["code"] == "HANDLE_ALREADY_USED"
    assert dispatch.dispatch_calls == 1


async def test_execute_operation_tool_unknown_shape_on_indeterminate_dispatch(
    loaded: sessionmaker[Session],
) -> None:
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="indeterminate",
            http_status=None,
            result=None,
            execution_id=None,
            correlation_available=False,
        )
    )
    server = make_server(loaded, dispatch=dispatch)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]
    with session_scope(loaded) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")

    result = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert result["operation_id"] == op_id
    assert result["state"] == "UNKNOWN"
    assert result["code"] == "DISPATCH_INDETERMINATE"
    assert "do not retry" in result["message"].lower() or "never retry" in result["message"].lower()
    assert result["correlation"] == {"available": False, "reason": "NO_EXECUTION_CORRELATION"}

    # UNKNOWN is terminal — the handle was already burned before dispatch, so a second
    # attempt is refused, not re-dispatched.
    second = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert second["error"]["code"] == "HANDLE_ALREADY_USED"
    assert dispatch.dispatch_calls == 1


async def test_execute_operation_tool_failed_shape_and_get_execution_log_names_the_node(
    loaded: sessionmaker[Session],
) -> None:
    """AC-15, through the MCP layer: a workflow that errors in n8n leaves the
    operation FAILED, and get_execution_log names the failing node and its error."""
    node_trace = {
        "nodes": [
            {
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "status": "success",
                "duration_ms": 3,
            },
            {
                "name": "HTTP Request",
                "type": "n8n-nodes-base.httpRequest",
                "status": "error",
                "duration_ms": 812,
            },
        ],
        "failed_node": "HTTP Request",
        "failed_node_error": "Request failed with status 422",
    }
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="error",
            http_status=422,
            result={"message": "Request failed with status 422"},
            execution_id="exec-2",
            correlation_available=True,
        ),
        node_trace=node_trace,
    )
    server = make_server(loaded, dispatch=dispatch)
    prepared = await call(
        server, "prepare_operation", workflow_id="wf.approval", arguments={"email": "a@b.com"}
    )
    op_id = prepared["operation_id"]
    with session_scope(loaded) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")

    result = await call(server, "execute_operation", operation_id=op_id, handle=op_id)
    assert result["state"] == "FAILED"
    assert result["error"]["body"] == {"message": "Request failed with status 422"}

    log = await call(server, "get_execution_log", operation_id=op_id)
    assert log["state"] == "FAILED"
    assert log["failed_node"] == "HTTP Request"
    node_names = [n["name"] for n in log["nodes"]]
    assert node_names == ["Webhook", "HTTP Request"]
