"""``list_environments`` (MCP_TOOLS.md section 5.9, ADR-016, stage 04) driven through
a full ``MCPServer.call_tool`` round trip — mirrors ``test_mcp_whoami_tool.py``'s
approach.
"""

from __future__ import annotations

import json
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
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: le-test
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


def make_server(
    session_factory: sessionmaker[Session], *, principal_id: str, enable_v2: bool = True
) -> MCPServer[Any]:
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=FakePreflight(),
        health=FakeHealth(),
        dispatch=FakeDispatch(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        enable_v2=enable_v2,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))
    return server


async def call_list_environments(server: MCPServer[Any]) -> dict[str, Any]:
    result = await server.call_tool("list_environments", {})
    assert isinstance(result, CallToolResult)
    assert not result.is_error, f"list_environments unexpectedly errored: {result.content}"
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


@pytest.mark.integration
async def test_list_environments_is_not_registered_in_v1_mode(
    session_factory: sessionmaker[Session],
) -> None:
    server = make_server(session_factory, principal_id="local", enable_v2=False)
    names = {t.name for t in await server.list_tools()}
    assert "list_environments" not in names


@pytest.mark.integration
async def test_list_environments_reports_safe_fields_only(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="staging",
            n8n_base_url_ref="env:SUPER_SECRET_URL_REF",
            n8n_api_key_ref="env:SUPER_SECRET_KEY_REF",
        )
        principal = PrincipalRepository(session).create(kind="user", display_name="Alice")
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        principal_id, env_id = principal.id, env.id

    server = make_server(loaded, principal_id=principal_id, enable_v2=True)
    payload = await call_list_environments(server)

    assert len(payload["environments"]) == 1
    row = payload["environments"][0]
    assert row["environment_id"] == env_id
    assert row["name"] == "staging"
    assert row["is_production"] is False
    assert row["archived"] is False
    assert isinstance(row["approval_policy_summary"], str)
    # Never a URL, a raw ref value, or anything resembling the secret references above.
    serialized = json.dumps(payload)
    assert "SUPER_SECRET" not in serialized
    assert "n8n_base_url_ref" not in serialized
    assert "n8n_api_key_ref" not in serialized


@pytest.mark.integration
async def test_list_environments_hides_archived_environment_from_a_non_admin(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="staging",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        EnvironmentRepository(session).archive(env.id)
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        admin = PrincipalRepository(session).create(kind="user", display_name="Admin")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        OrganizationMembershipRepository(session).create(
            principal_id=admin.id, organization_id=org.id, roles=["admin"], workflow_scope="*"
        )
        viewer_id, admin_id, env_id = viewer.id, admin.id, env.id

    viewer_server = make_server(loaded, principal_id=viewer_id, enable_v2=True)
    viewer_payload = await call_list_environments(viewer_server)
    assert viewer_payload["environments"] == []

    admin_server = make_server(loaded, principal_id=admin_id, enable_v2=True)
    admin_payload = await call_list_environments(admin_server)
    assert len(admin_payload["environments"]) == 1
    assert admin_payload["environments"][0]["environment_id"] == env_id
    assert admin_payload["environments"][0]["archived"] is True
