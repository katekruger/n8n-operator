"""Named GTM scenarios (ARCHITECTURE.md section 11.1, stage 04's own completion
gate) driven through the real MCP tool layer — the same ``MCPServer.call_tool``
round trip a real client would make.

Scenario 1 mirrors section 11.1's own worked journey: a startup GTM engineer with
`operator` scoped to `crm.*` in both `staging` and `production`, sees implicit-
environment refusal (AC-37) and a workflow whose approval policy differs by
environment for the identical workflow ID — via a strengthen-only overlay (ADR-016
rule R14), the actual mechanism this stage implements: staging (and the base
registry) leave `crm.sync_contact` auto-approved; production's overlay requires a
human's approval for it.

Scenario 2 is the plan's own named RevOps case in miniature (bulk CRM update
restricted to a narrower environment scope than read/enrichment access) and the
named "a marketing operator cannot infer or address a sales-only production
workflow" case: `describe_workflow` against a workflow outside their `workflow_scope`
returns `WORKFLOW_NOT_FOUND` — identical to a nonexistent workflow ID (invariant I14).
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
from n8n_operator.core.models import PreflightResult
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
  name: gtm-scenarios
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
    description: Enrichment — open to every environment.
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
  - id: sales.close_deal
    n8n_workflow_id: n8n-2
    title: Close a sales deal
    description: Sales-only; a marketing operator has no scope over this at all.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: high
    side_effects: irreversible
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
""".format(hash_a="a" * 64, hash_b="b" * 64)

PRODUCTION_OVERLAY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: production-overlay
overlays:
  - workflow_id: crm.sync_contact
    approval_override: required
"""


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


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


def _make_server(session_factory: sessionmaker[Session], *, principal_id: str) -> MCPServer[Any]:
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


@pytest.mark.integration
async def test_startup_gtm_engineer_operating_staging_and_production(
    loaded: sessionmaker[Session], tmp_path: Path
) -> None:
    """ARCHITECTURE.md section 11.1."""
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Startup")
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
        engineer = PrincipalRepository(session).create(kind="user", display_name="GTM Engineer")
        OrganizationMembershipRepository(session).create(
            principal_id=engineer.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="crm.*",
            environment_scope=["*"],
        )
        engineer_id, staging_id, production_id = engineer.id, staging.id, production.id

    overlay_path = tmp_path / "production.yaml"
    overlay_path.write_text(PRODUCTION_OVERLAY_YAML)
    with session_scope(loaded) as session:
        service.reload_overlay(session, overlay_path, environment_id=production_id)

    server = _make_server(loaded, principal_id=engineer_id)

    # 2. list_environments — staging and production, neither archived.
    environments = (await _call(server, "list_environments"))["environments"]
    names = {e["name"] for e in environments}
    assert names == {"staging", "production"}
    assert all(not e["archived"] for e in environments)

    # 3. prepare_operation with no `environment` — ENVIRONMENT_REQUIRED: two
    # environments exist, and production is never implicit (ADR-016 section 3).
    no_env_result = await _call(
        server, "prepare_operation", workflow_id="crm.sync_contact", arguments={}
    )
    assert no_env_result["error"]["code"] == "ENVIRONMENT_REQUIRED"

    # Naming staging: the base registry's own `approval: none` applies, unchanged —
    # executes without waiting.
    staging_result = await _call(
        server,
        "prepare_operation",
        workflow_id="crm.sync_contact",
        arguments={},
        environment=staging_id,
    )
    assert staging_result["state"] == "APPROVED"
    assert staging_result["environment"] == staging_id

    # 4. The same call against production — the overlay strengthens approval to
    # required for this one environment; the identical workflow ID behaves
    # differently purely because of which environment it targets.
    production_result = await _call(
        server,
        "prepare_operation",
        workflow_id="crm.sync_contact",
        arguments={},
        environment=production_id,
    )
    assert production_result["state"] == "PENDING_APPROVAL"
    assert production_result["environment"] == production_id


@pytest.mark.integration
async def test_marketing_operator_cannot_infer_a_sales_only_production_workflow(
    loaded: sessionmaker[Session],
) -> None:
    """The plan's own named case: a marketing operator's ``describe_workflow``
    against a sales-only workflow returns ``WORKFLOW_NOT_FOUND`` — identical to a
    nonexistent workflow ID (invariant I14), never a signal that the workflow exists
    but is off-limits."""
    with session_scope(loaded) as session:
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
            is_production=True,
        )
        marketing_operator = PrincipalRepository(session).create(
            kind="user", display_name="Marketing Operator"
        )
        OrganizationMembershipRepository(session).create(
            principal_id=marketing_operator.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="crm.*",  # never sales.*
        )
        marketing_id, env_id = marketing_operator.id, env.id

    server = _make_server(loaded, principal_id=marketing_id)

    real_workflow_result = await _call(
        server, "describe_workflow", workflow_id="sales.close_deal", environment=env_id
    )
    nonexistent_result = await _call(
        server, "describe_workflow", workflow_id="does.not.exist", environment=env_id
    )
    assert real_workflow_result["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert real_workflow_result == nonexistent_result

    # The same operator's own crm.* scope still works, in the same environment.
    own_scope_result = await _call(
        server, "describe_workflow", workflow_id="crm.sync_contact", environment=env_id
    )
    assert own_scope_result["workflow_id"] == "crm.sync_contact"


@pytest.mark.integration
async def test_revops_bulk_update_restricted_to_a_narrower_environment_than_enrichment(
    loaded: sessionmaker[Session],
) -> None:
    """The plan's own named RevOps case, in miniature: enrichment (``crm.sync_
    contact``) is open in every environment a RevOps operator can reach, while a more
    sensitive capability is only ever reachable in one of them — modeled here as an
    `operator` grant whose `environment_scope` is narrowed to `staging` only, so the
    identical `crm.*` workflow_scope still refuses production."""
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
        revops = PrincipalRepository(session).create(kind="user", display_name="RevOps")
        OrganizationMembershipRepository(session).create(
            principal_id=revops.id,
            organization_id=org.id,
            roles=["operator"],
            workflow_scope="crm.*",
            environment_scope=[staging.id],
        )
        revops_id, staging_id, production_id = revops.id, staging.id, production.id

    server = _make_server(loaded, principal_id=revops_id)

    staging_result = await _call(
        server, "describe_workflow", workflow_id="crm.sync_contact", environment=staging_id
    )
    assert staging_result["workflow_id"] == "crm.sync_contact"

    production_result = await _call(
        server, "describe_workflow", workflow_id="crm.sync_contact", environment=production_id
    )
    assert production_result["error"]["code"] == "WORKFLOW_NOT_FOUND"
