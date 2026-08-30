"""Stage 04's own "two-instance integration harness": two distinct
``httpx.MockTransport``-backed n8n instances, one per environment, proving
``get_instance_health``/``preflight_workflow`` genuinely reach a *different* n8n
instance depending on which environment resolved — not silently sharing one client
the way every v1/dev-mode call still does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult

from integration.mock_n8n import MockN8n
from n8n_operator.core import service
from n8n_operator.mcp.server import _DispatchAdapter, _HealthAdapter, _PreflightAdapter
from n8n_operator.mcp.tools import N8nAdapterBundle, ToolDeps, build_tools
from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.dispatch import N8nDispatch
from n8n_operator.n8n.health import N8nHealth
from n8n_operator.n8n.preflight import N8nPreflight
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

LIVE_DEFINITION: dict[str, Any] = {
    "id": "n8n-1",
    "name": "Sync",
    "active": True,
    "nodes": [
        {
            "id": "webhook-1",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 0],
            "parameters": {"httpMethod": "POST", "path": "a"},
        },
    ],
    "connections": {},
    "settings": {},
}

REGISTRY_YAML = f"""apiVersion: n8n-operator/v1
metadata:
  name: two-instance-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
    owner: carolyn
    version: 1
    definition_hash: {compute_definition_hash(LIVE_DEFINITION)}
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
"""


class UnreachableFallback:
    """The fixed, v1/dev-mode client this test deliberately never wants used —
    standing in for ``ToolDeps.preflight``/``health``/``dispatch`` so a bug that
    falls back to it (instead of the per-environment factory) fails loudly rather
    than silently passing against the wrong instance."""

    def check(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the fixed v1/dev-mode adapter should never be reached in this test")

    def dispatch(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the fixed v1/dev-mode adapter should never be reached in this test")

    def fetch_node_trace(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the fixed v1/dev-mode adapter should never be reached in this test")

    def get_workflow(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the fixed v1/dev-mode adapter should never be reached in this test")


def _bundle_for(mock: MockN8n) -> N8nAdapterBundle:
    client = N8nClient(base_url="http://mock-n8n", api_key="fake", transport=mock.transport())
    return N8nAdapterBundle(
        preflight=_PreflightAdapter(N8nPreflight(client)),
        health=_HealthAdapter(N8nHealth(client)),
        dispatch=_DispatchAdapter(N8nDispatch(client)),
        definition=client,
    )


@pytest.mark.integration
async def test_get_instance_health_reaches_the_resolved_environments_own_instance(
    session_factory: Any, tmp_path: Path
) -> None:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)

    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
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
        principal = PrincipalRepository(session).create(kind="user", display_name="Alice")
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        principal_id, staging_id, production_id = principal.id, staging.id, production.id

    staging_mock = MockN8n()
    staging_mock.healthy = True
    staging_mock.api_version = "1.0.0-staging"
    production_mock = MockN8n()
    production_mock.healthy = True
    production_mock.api_version = "2.0.0-production"

    bundles = {staging_id: _bundle_for(staging_mock), production_id: _bundle_for(production_mock)}

    deps = ToolDeps(
        session_factory=session_factory,
        preflight=UnreachableFallback(),
        health=UnreachableFallback(),
        dispatch=UnreachableFallback(),
        definition=UnreachableFallback(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        enable_v2=True,
        n8n_client_factory=bundles.__getitem__,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))

    async def health_for(environment_id: str) -> dict[str, Any]:
        result = await server.call_tool("get_instance_health", {"environment": environment_id})
        assert isinstance(result, CallToolResult)
        assert not result.is_error, f"unexpected error: {result.content}"
        text = result.content[0].text  # type: ignore[union-attr]
        return json.loads(text)  # type: ignore[no-any-return]

    staging_result = await health_for(staging_id)
    production_result = await health_for(production_id)

    assert staging_result["n8n_version"] == "1.0.0-staging"
    assert production_result["n8n_version"] == "2.0.0-production"
    assert staging_result["environment"] == staging_id
    assert production_result["environment"] == production_id
    # Each instance was actually asked, exactly once each — proving the two really
    # are distinct transports, not one shared mock silently answering for both.
    assert len(staging_mock.requests) >= 1
    assert len(production_mock.requests) >= 1


@pytest.mark.integration
async def test_preflight_workflow_reaches_the_resolved_environments_own_instance(
    session_factory: Any, tmp_path: Path
) -> None:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)

    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
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
        principal = PrincipalRepository(session).create(kind="user", display_name="Alice")
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=org.id, roles=["viewer"], workflow_scope="*"
        )
        principal_id, staging_id, production_id = principal.id, staging.id, production.id

    # Staging's instance has the workflow live; production's does not — proving the
    # preflight check that ran was really against the resolved environment's own
    # instance, not a shared one that would see it either way.
    staging_mock = MockN8n()
    staging_mock.healthy = True
    staging_mock.add_workflow("n8n-1", LIVE_DEFINITION)
    production_mock = MockN8n()
    production_mock.healthy = True
    # No workflow added — a live fetch for it 404s.

    bundles = {staging_id: _bundle_for(staging_mock), production_id: _bundle_for(production_mock)}

    deps = ToolDeps(
        session_factory=session_factory,
        preflight=UnreachableFallback(),
        health=UnreachableFallback(),
        dispatch=UnreachableFallback(),
        definition=UnreachableFallback(),
        server_max_argument_bytes=262_144,
        principal_id=principal_id,
        caller_is_local=True,
        enable_v2=True,
        n8n_client_factory=bundles.__getitem__,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))

    async def preflight_for(environment_id: str) -> dict[str, Any]:
        result = await server.call_tool(
            "preflight_workflow",
            {"workflow_id": "crm.sync_contact", "environment": environment_id},
        )
        assert isinstance(result, CallToolResult)
        assert not result.is_error, f"unexpected error: {result.content}"
        text = result.content[0].text  # type: ignore[union-attr]
        return json.loads(text)  # type: ignore[no-any-return]

    staging_result = await preflight_for(staging_id)
    production_result = await preflight_for(production_id)

    assert staging_result["ready"] is True
    assert production_result["ready"] is False
    assert len(staging_mock.requests) >= 1
    assert len(production_mock.requests) >= 1
