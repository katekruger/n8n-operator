"""``diff_workflow_definition`` (stage 07, MCP_TOOLS.md section 5.6) driven through a
full ``MCPServer.call_tool`` round trip — mirroring ``test_mcp_whoami_tool.py``'s
approach for its own v2-only tool, so a regression in the manual ``Tool`` construction
or the composition-root wiring (``ToolDeps.definition``/``N8nAdapterBundle.definition``)
shows up here exactly as it would to a real client.

``core.service.diff_workflow_definition`` itself is exhaustively tested at the service
layer (``tests/integration/test_definition_diff_service.py``) — this file exists only to
prove the MCP-level plumbing: registration under ``enable_v2``, argument/result shape,
and that a real end-to-end call through the manually-constructed ``Tool`` produces the
same result the service function would.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
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
    WorkflowDefinitionSnapshotRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: diff-tool-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-real-1
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

_REGISTERED_RAW: dict[str, Any] = {
    "id": "n8n-real-1",
    "name": "Sync a contact",
    "active": True,
    "nodes": [
        {
            "id": "node-1",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "position": [0, 0],
            "parameters": {},
        },
    ],
    "connections": {},
    "settings": {},
    "pinData": {},
}


class FakePreflight:
    def check(self, workflow: Any) -> Any:
        raise NotImplementedError  # diff_workflow_definition never touches preflight


class FakeHealth:
    def check(self) -> Any:
        raise NotImplementedError


class FakeDispatch:
    def dispatch(self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int) -> Any:
        raise NotImplementedError

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class FakeDefinition:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        return self._raw


def make_server(
    session_factory: sessionmaker[Session],
    *,
    principal_id: str,
    definition: FakeDefinition,
    enable_v2: bool = True,
) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=FakePreflight(),
        health=FakeHealth(),
        dispatch=FakeDispatch(),
        definition=definition,
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        approval_base_url="http://127.0.0.1:8765",
        enable_v2=enable_v2,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))
    return server


@pytest.fixture
def loaded(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflows.yaml"
            path.write_text(REGISTRY_YAML)
            service.reload_registry(session, path, server_max_argument_bytes=262_144)
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


async def call_diff(server: MCPServer[Any], **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool("diff_workflow_definition", arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, f"diff_workflow_definition unexpectedly errored: {result.content}"
    assert len(result.content) == 1
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


@pytest.mark.integration
async def test_diff_workflow_definition_is_not_registered_in_v1_mode(
    session_factory: sessionmaker[Session],
) -> None:
    server = make_server(
        session_factory,
        principal_id="local",
        definition=FakeDefinition(_REGISTERED_RAW),
        enable_v2=False,
    )
    names = {t.name for t in await server.list_tools()}
    assert "diff_workflow_definition" not in names


@pytest.mark.integration
async def test_diff_workflow_definition_with_no_snapshot_gives_an_honest_hash_comparison(
    loaded: sessionmaker[Session],
) -> None:
    live = {**_REGISTERED_RAW}
    server = make_server(loaded, principal_id="viewer-1", definition=FakeDefinition(live))
    result = await call_diff(server, workflow_id="crm.sync_contact")
    assert result["diff_available"] is False
    assert result["diff"] == []
    assert isinstance(result["changed"], bool)
    assert result["note"] is not None
    assert result["registered_hash"].startswith("sha256:")
    assert result["live_hash"].startswith("sha256:")


@pytest.mark.integration
async def test_diff_workflow_definition_with_a_captured_snapshot_returns_a_real_diff(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        WorkflowDefinitionSnapshotRepository(session).create(
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            canonical_definition={
                "nodes": [
                    {k: v for k, v in n.items() if k != "position"}
                    for n in _REGISTERED_RAW["nodes"]
                ],
                "connections": _REGISTERED_RAW["connections"],
                "settings": _REGISTERED_RAW["settings"],
            },
            captured_by="local",
        )

    live = {
        **_REGISTERED_RAW,
        "nodes": [
            {**_REGISTERED_RAW["nodes"][0], "parameters": {"url": "https://new.example.com"}}
        ],
    }
    server = make_server(loaded, principal_id="viewer-1", definition=FakeDefinition(live))
    result = await call_diff(server, workflow_id="crm.sync_contact")
    assert result["diff_available"] is True
    assert result["changed"] is True
    assert len(result["diff"]) == 1
    assert result["diff"][0]["path"] == "/nodes/0/parameters/url"


@pytest.mark.integration
async def test_diff_workflow_definition_unknown_workflow_id_is_not_found(
    loaded: sessionmaker[Session],
) -> None:
    server = make_server(
        loaded, principal_id="viewer-1", definition=FakeDefinition(_REGISTERED_RAW)
    )
    result = await server.call_tool("diff_workflow_definition", {"workflow_id": "does.not.exist"})
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    body = json.loads(text)
    assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
