"""The loopback approval app's v2 per-approver token isolation (stage 05, ADR-017) —
each eligible approver gets their own token, bound to their own identity, and can
only ever decide as themselves. Extends ``tests/integration/test_approval_app.py``'s
v1-only coverage to the quorum-2 case.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.approval.app import build_app
from n8n_operator.core import service
from n8n_operator.core.handles import compute_approval_binding, mint_approval_token
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import (
    ApprovalRepository,
    EnvironmentRepository,
    OperationRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

APPROVAL_BIND = "127.0.0.1:8765"
EXPECTED_ORIGIN = f"http://{APPROVAL_BIND}"

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: quorum-app-test
workflows:
  - id: crm.bulk_update_stage
    n8n_workflow_id: n8n-1
    title: Bulk-update deal stage
    description: Production-only, two-approver.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: high
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
      quorum_count: 2
""".format(hash_a="a" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def world(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
            is_production=True,
        )
        requester = PrincipalRepository(session).create(kind="user", display_name="Requester")
        approver_a = PrincipalRepository(session).create(kind="user", display_name="Approver A")
        approver_b = PrincipalRepository(session).create(kind="user", display_name="Approver B")
        memberships = OrganizationMembershipRepository(session)
        memberships.create(principal_id=requester.id, organization_id=org.id, roles=["operator"])
        memberships.create(principal_id=approver_a.id, organization_id=org.id, roles=["approver"])
        memberships.create(principal_id=approver_b.id, organization_id=org.id, roles=["approver"])
        return {
            "env_id": env.id,
            "requester_id": requester.id,
            "approver_a": approver_a.id,
            "approver_b": approver_b.id,
        }


def _prepare(session_factory: sessionmaker[Session], world: dict[str, Any]) -> str:
    """Prepare ``crm.bulk_update_stage`` (quorum 2) and return the operation ID.
    Deliberately does not call ``request_approval`` — a real per-approver web token
    is minted directly via :func:`_mint_web_token` per test, so each test controls
    exactly which principals hold a token without ``request_approval``'s own
    get-or-create idempotency interacting with a second, independently-minted row
    for the same principal."""
    with session_scope(session_factory) as session:
        operation, _, _ = service.prepare_operation(
            session,
            principal_id=world["requester_id"],
            environment=world["env_id"],
            workflow_id="crm.bulk_update_stage",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        return operation.id


def _mint_web_token(
    session_factory: sessionmaker[Session], *, operation_id: str, principal_id: str
) -> str:
    """A raw, working per-approver token for ``principal_id`` on ``operation_id``.

    ``request_approval``'s own per-approver row (``core.service.
    _get_or_mint_own_approval_row``) never surfaces the raw token anywhere a test —
    or an MCP tool result, or a CLI print statement — could read it back (only its
    hash is ever persisted, and ``NotificationEvent`` deliberately never carries
    operation content, ADR-018 section 4, boundary B4: the agent must never be
    handed something that lets it approve). A real per-approver web link is
    therefore something only a human-facing surface outside this codebase's current
    scope would construct; this helper mints and binds one exactly the way
    ``_get_or_mint_own_approval_row`` does internally, so the web app's own
    ``assigned_to``-based decision logic (the part stage 05 actually changed) can be
    exercised end to end.
    """
    with session_scope(session_factory) as session:
        row = OperationRepository(session).get(operation_id)
        assert row is not None
        assert row.approval_expires_at is not None
        minted = mint_approval_token()
        binding_hash = compute_approval_binding(
            operation_id=row.id,
            principal_id=principal_id,
            argument_fingerprint=row.argument_fingerprint,
            snapshot_id=row.snapshot_id,
            definition_hash=row.definition_hash,
        )
        ApprovalRepository(session).create(
            operation_id=row.id,
            token_hash=minted.token_hash,
            binding_hash=binding_hash,
            expires_at=row.approval_expires_at,
            assigned_to=principal_id,
        )
        return minted.token


@pytest.fixture
def client(session_factory: sessionmaker[Session], world: dict[str, Any]) -> Iterator[TestClient]:
    app = build_app(APPROVAL_BIND, session_factory, enable_v2=True)
    with TestClient(app, base_url=EXPECTED_ORIGIN) as test_client:
        yield test_client


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None, html
    return match.group(1)


def _approve(client: TestClient, token: str) -> Any:
    page = client.get(f"/approve/{token}")
    assert page.status_code == 200
    csrf = _extract_csrf(page.text)
    return client.post(
        f"/approve/{token}",
        data={"csrf_token": csrf},
        headers={"origin": EXPECTED_ORIGIN},
    )


def test_each_approver_token_decides_only_as_its_own_principal(
    client: TestClient, session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    token_a = _mint_web_token(
        session_factory, operation_id=operation_id, principal_id=world["approver_a"]
    )
    response = _approve(client, token_a)
    assert response.status_code == 200
    assert world["approver_a"] in response.text
    assert "1 of 2 decided" in response.text

    with session_scope(session_factory) as session:
        status = service.get_approval_status(
            session,
            operation_id=operation_id,
            principal_id=world["requester_id"],
            enable_v2=True,
        )
    assert [d.principal_id for d in status.decisions] == [world["approver_a"]]
    assert status.ready is False


def test_second_approver_own_token_reaches_quorum_independently(
    client: TestClient, session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    operation_id = _prepare(session_factory, world)
    token_a = _mint_web_token(
        session_factory, operation_id=operation_id, principal_id=world["approver_a"]
    )
    token_b = _mint_web_token(
        session_factory, operation_id=operation_id, principal_id=world["approver_b"]
    )
    _approve(client, token_a)
    response = _approve(client, token_b)
    assert response.status_code == 200

    with session_scope(session_factory) as session:
        status = service.get_approval_status(
            session,
            operation_id=operation_id,
            principal_id=world["requester_id"],
            enable_v2=True,
        )
    assert status.ready is True
    assert {d.principal_id for d in status.decisions} == {
        world["approver_a"],
        world["approver_b"],
    }


def test_a_tokens_second_use_is_rejected_as_already_used(
    client: TestClient, session_factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    """A token is single-use — structurally, not merely by convention — so it can
    never be replayed to cast a second vote as the same approver, let alone forged
    to impersonate a different one (the binding includes ``assigned_to``)."""
    operation_id = _prepare(session_factory, world)
    token_a = _mint_web_token(
        session_factory, operation_id=operation_id, principal_id=world["approver_a"]
    )
    first = _approve(client, token_a)
    assert first.status_code == 200

    second_page = client.get(f"/approve/{token_a}")
    assert second_page.status_code == 409
    assert "already been used" in second_page.text
