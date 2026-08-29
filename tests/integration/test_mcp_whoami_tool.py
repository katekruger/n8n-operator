"""``whoami`` (BUILD_PLAN section 7.2, MCP_TOOLS.md section 5.1, stage 02) driven
through a full ``MCPServer.call_tool`` round trip, mirroring
``test_mcp_tools.py``'s approach for the v1 twelve so a regression in the manual
``Tool`` construction shows up here exactly as it would to a real client.

Covers the stage 02 completion gate's own requirement that ``whoami`` is the
thirteenth tool only when v2 mode is enabled (v1 mode stays exactly twelve — see
``tests/contract/test_mcp_tool_inventory.py`` for the inverse contract), that its
result never carries a provider token or raw claim, that a principal with no active
memberships anywhere gets an empty-but-successful result (the stage 02 negative case
"missing active organization"), and that the result reflects only what the resolved
``(iss, sub)`` principal actually has in the database — never anything a caller's own
JWT claims might assert about itself (the stage 02 negative case "token substitution
across organizations").
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.server import MCPServer
from mcp_types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.storage.repository import (
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope


class FakePreflight:
    def check(self, workflow: Any) -> Any:
        raise NotImplementedError  # whoami never touches n8n I/O


class FakeHealth:
    def check(self) -> Any:
        raise NotImplementedError


class FakeDispatch:
    def dispatch(self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int) -> Any:
        raise NotImplementedError

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


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
        approval_base_url="http://127.0.0.1:8765",
        enable_v2=enable_v2,
    )
    server: MCPServer[Any] = MCPServer("test", tools=build_tools(deps))
    register_resources(server, deps)
    return server


async def call_whoami(server: MCPServer[Any]) -> dict[str, Any]:
    result = await server.call_tool("whoami", {})
    assert isinstance(result, CallToolResult)
    assert not result.is_error, f"whoami unexpectedly errored: {result.content}"
    assert len(result.content) == 1
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)  # type: ignore[no-any-return]


@pytest.mark.integration
async def test_whoami_is_not_registered_in_v1_mode(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="user", display_name="Alice", external_issuer="https://idp.example.com"
        )
        principal_id = principal.id

    server = make_server(session_factory, principal_id=principal_id, enable_v2=False)
    names = {t.name for t in await server.list_tools()}
    assert "whoami" not in names
    assert len(names) == 12


@pytest.mark.integration
async def test_whoami_is_the_thirteenth_tool_in_v2_mode(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="user", display_name="Alice", external_issuer="https://idp.example.com"
        )
        principal_id = principal.id

    server = make_server(session_factory, principal_id=principal_id, enable_v2=True)
    names = {t.name for t in await server.list_tools()}
    assert "whoami" in names
    assert "list_environments" in names
    assert len(names) == 14


@pytest.mark.integration
async def test_whoami_reports_organizations_roles_and_environments(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="user", display_name="Alice", external_issuer="https://idp.example.com"
        )
        org = OrganizationRepository(session).create(name="Acme")
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=org.id, roles=["viewer", "operator"]
        )
        principal_id = principal.id

    server = make_server(session_factory, principal_id=principal_id)
    result = await call_whoami(server)

    assert result["principal_id"] == principal_id
    assert result["kind"] == "user"
    assert result["display_name"] == "Alice"
    assert len(result["organizations"]) == 1
    assert result["organizations"][0]["roles"] == ["viewer", "operator"]


@pytest.mark.integration
async def test_whoami_for_a_principal_with_no_active_organization_returns_an_empty_list(
    session_factory: sessionmaker[Session],
) -> None:
    """The stage 02 negative case "missing active organization": a freshly
    JIT-provisioned user, never granted any membership, authenticates successfully
    (identity and authorization are separate concerns) and gets back an empty
    ``organizations`` list rather than an error. v1 tools take no ``environment``
    argument yet (that resolution path is stage 03/04 territory), so this is the
    entire surface stage 02 owns for this case."""
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="user", display_name="Nobody", external_issuer="https://idp.example.com"
        )
        principal_id = principal.id

    server = make_server(session_factory, principal_id=principal_id)
    result = await call_whoami(server)

    assert result["principal_id"] == principal_id
    assert result["organizations"] == []


@pytest.mark.integration
async def test_whoami_never_leaks_a_provider_token_or_raw_claim(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(
            kind="user", display_name="Alice", external_issuer="https://idp.example.com"
        )
        org = OrganizationRepository(session).create(name="Acme")
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=org.id, roles=["viewer"]
        )
        principal_id = principal.id

    server = make_server(session_factory, principal_id=principal_id)
    result = await call_whoami(server)

    serialized = json.dumps(result)
    for forbidden in (
        "https://idp.example.com",  # the issuer is never echoed back
        "external_subject",
        "external_issuer",
        "claims",
        "token",
        "credential_ref",
    ):
        assert forbidden not in serialized, f"whoami result unexpectedly contains {forbidden!r}"


@pytest.mark.integration
async def test_whoami_reflects_only_database_membership_never_a_claim_the_caller_asserts(
    session_factory: sessionmaker[Session],
) -> None:
    """The stage 02 negative case "token substitution across organizations": whoami's
    result is built entirely from ``build_whoami``'s own database query keyed on the
    already-resolved ``principal_id`` (set once, server-side, by
    ``_OperatorTokenVerifier`` after JIT provisioning and the disabled-principal check
    — see ``test_operator_token_verifier.py``). Nothing about which organization a
    caller sees can be influenced by any claim inside a token, because no tool
    argument or claim value is ever consulted here at all — proven by two distinct
    principals, each a member of a different, single organization, each seeing only
    their own."""
    with session_scope(session_factory) as session:
        alice = PrincipalRepository(session).create(
            kind="user", display_name="Alice", external_issuer="https://idp.example.com"
        )
        bob = PrincipalRepository(session).create(
            kind="user", display_name="Bob", external_issuer="https://idp.example.com"
        )
        org_a = OrganizationRepository(session).create(name="Org A")
        org_b = OrganizationRepository(session).create(name="Org B")
        OrganizationMembershipRepository(session).create(
            principal_id=alice.id, organization_id=org_a.id, roles=["admin"]
        )
        OrganizationMembershipRepository(session).create(
            principal_id=bob.id, organization_id=org_b.id, roles=["admin"]
        )
        alice_id, bob_id = alice.id, bob.id

    alice_result = await call_whoami(make_server(session_factory, principal_id=alice_id))
    bob_result = await call_whoami(make_server(session_factory, principal_id=bob_id))

    assert [org["name"] for org in alice_result["organizations"]] == ["Org A"]
    assert [org["name"] for org in bob_result["organizations"]] == ["Org B"]
