"""``get_metrics``/``list_audit_events`` (stage 08, MCP_TOOLS.md sections 5.7-5.8)
driven through a full ``MCPServer.call_tool`` round trip — mirroring
``test_mcp_diff_workflow_definition_tool.py``'s approach for stage 07's own v2-only
tool, so a regression in the manual ``Tool`` construction shows up here exactly as it
would to a real client.

``core.service.get_metrics``/``list_audit_events`` themselves are exhaustively tested
at the service layer (``tests/integration/test_metrics_audit_service.py``) — this file
exists only to prove the MCP-level plumbing: registration under ``enable_v2``,
argument/result shape.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
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
  name: metrics-audit-tool-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
    description: External write.
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


class FakePreflight:
    def check(self, workflow: Any) -> Any:
        raise NotImplementedError


class FakeHealth:
    def check(self) -> Any:
        raise NotImplementedError


class FakeDispatch:
    def dispatch(self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int) -> Any:
        raise NotImplementedError

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class FakeDefinition:
    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        raise NotImplementedError


def make_server(
    session_factory: sessionmaker[Session], *, principal_id: str, enable_v2: bool = True
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
        approval_base_url="http://127.0.0.1:8765",
        enable_v2=enable_v2,
    )
    return MCPServer("test", tools=build_tools(deps))


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], tmp_path: Any) -> sessionmaker[Session]:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        PrincipalRepository(session).create(id="viewer-1", kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id="viewer-1", organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
    return session_factory


async def call(server: MCPServer[Any], name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, f"{name} unexpectedly errored: {result.content}"
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


@pytest.mark.integration
async def test_get_metrics_is_not_registered_in_v1_mode(
    session_factory: sessionmaker[Session],
) -> None:
    server = make_server(session_factory, principal_id="local", enable_v2=False)
    names = {t.name for t in await server.list_tools()}
    assert "get_metrics" not in names


@pytest.mark.integration
async def test_list_audit_events_is_not_registered_in_v1_mode(
    session_factory: sessionmaker[Session],
) -> None:
    server = make_server(session_factory, principal_id="local", enable_v2=False)
    names = {t.name for t in await server.list_tools()}
    assert "list_audit_events" not in names


@pytest.mark.integration
async def test_get_metrics_returns_the_documented_shape(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, principal_id="viewer-1")
    result = await call(server, "get_metrics")
    assert set(result.keys()) == {
        "environment",
        "window",
        "generated_at",
        "totals",
        "latency_ms",
        "breakdown",
    }
    assert result["window"] == "24h"
    assert result["totals"] == {"count": 0, "by_outcome": {}}
    assert result["latency_ms"]["p50"] is None
    assert result["latency_ms"]["p50_reason"] == "insufficient_sample"


@pytest.mark.integration
async def test_get_metrics_rejects_a_bad_window(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, principal_id="viewer-1")
    result = await server.call_tool("get_metrics", {"window": "90d"})
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    body = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.integration
async def test_list_audit_events_returns_the_documented_shape(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(loaded, principal_id="viewer-1")
    result = await call(server, "list_audit_events")
    assert set(result.keys()) == {"events", "next_cursor"}
    assert isinstance(result["events"], list)


@pytest.mark.integration
async def test_list_audit_events_rejects_a_bad_limit(loaded: sessionmaker[Session]) -> None:
    server = make_server(loaded, principal_id="viewer-1")
    result = await server.call_tool("list_audit_events", {"limit": 0})
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    body = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.integration
async def test_list_audit_events_never_leaks_another_orgs_operation_through_mcp(
    session_factory: sessionmaker[Session], tmp_path: Any
) -> None:
    """Full ``MCPServer.call_tool`` round trip (Stage 11 security review) — proves the
    fix holds at the actual client-facing boundary, not only at the service/repository
    layers. Org B's ``"*"``-scoped viewer must never see Org A's operation, in *either*
    of the two ways a leak could show up: as a present event (the confirmed bug this
    stage fixed) or as a present-but-redacted stand-in (the anti-enumeration shape this
    codebase already uses everywhere else, e.g. ``WORKFLOW_NOT_FOUND`` never
    distinguishing "doesn't exist" from "you can't see it" — an absent row is the only
    correct shape, never a placeholder that confirms the row's existence)."""
    from datetime import UTC, datetime

    from n8n_operator.core import service as core_service
    from n8n_operator.core.models import PreflightResult

    class ReadyPreflight:
        def check(self, workflow: Any) -> Any:
            return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))

    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    with session_scope(session_factory) as session:
        core_service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
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
        OrganizationMembershipRepository(session).create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_a.id],
        )
        OrganizationMembershipRepository(session).create(
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=["*"],
        )
        operator_a_id, viewer_b_id, env_a_id, env_b_id = (
            operator_a.id,
            viewer_b.id,
            env_a.id,
            env_b.id,
        )

    with session_scope(session_factory) as session:
        operation, _replay, _token = core_service.prepare_operation(
            session,
            principal_id=operator_a_id,
            environment=env_a_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=ReadyPreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        org_a_operation_id = operation.id

    server = make_server(session_factory, principal_id=viewer_b_id)
    result = await call(server, "list_audit_events", environment=env_b_id, limit=100)
    subject_ids = {event["subject_id"] for event in result["events"]}
    assert org_a_operation_id not in subject_ids
    # Not present at all — never present with a redacted/placeholder detail either,
    # which would itself be an enumeration oracle.
    for event in result["events"]:
        assert event["subject_id"] != org_a_operation_id


def test_get_metrics_and_list_audit_events_never_reference_a_raw_n8n_identifier() -> None:
    """Static source check (mirrors stage 07's "no gating path imports
    definition_diff" technique): ``get_metrics``'s breakdown keys are always a
    *registry* workflow id or a fixed enum value (risk/side_effects/outcome), and
    ``list_audit_events``'s ``detail`` is already write-time redacted — neither
    function has any legitimate reason to reference ``n8n_workflow_id``, a credential
    reference, or an instance URL/API key field. A future edit that starts reading
    one of those would defeat boundary B1/B5 silently; this fails loudly instead."""
    import inspect

    from n8n_operator.core import service as service_module

    for name in ("get_metrics", "list_audit_events"):
        source = inspect.getsource(getattr(service_module, name))
        for forbidden in (
            "n8n_workflow_id",
            "n8n_base_url",
            "n8n_api_key",
            "credential_ref",
            "webhook_path",
        ):
            assert forbidden not in source, f"{name} references {forbidden!r}"
