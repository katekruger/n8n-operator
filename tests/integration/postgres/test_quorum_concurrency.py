"""Two eligible approvers deciding the *same* quorum-2 operation concurrently, on
two independent database connections, against a real PostgreSQL instance (stage 05,
ADR-017) — proving ``OperationRepository.get_for_update``'s row lock actually
serializes the tally so exactly one ``T06`` fires and neither vote is lost, the race
SQLite's single-writer semantics cannot exercise (see ``core.service.approve_operation``'s
own docstring on why the re-fetch happens *after* the write, under a lock).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: quorum-concurrency-test
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


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url, pool_size=5, max_overflow=5)


def test_concurrent_approvals_from_two_approvers_reach_quorum_exactly_once(
    postgres_test_db_url: str, tmp_path: Path
) -> None:
    engine = _migrated_engine(postgres_test_db_url)
    try:
        factory = create_session_factory(engine)

        registry_path = tmp_path / "workflows.yaml"
        registry_path.write_text(REGISTRY_YAML)
        with session_scope(factory) as session:
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
            approver_a = PrincipalRepository(session).create(kind="user", display_name="A")
            approver_b = PrincipalRepository(session).create(kind="user", display_name="B")
            memberships = OrganizationMembershipRepository(session)
            memberships.create(
                principal_id=requester.id, organization_id=org.id, roles=["operator"]
            )
            memberships.create(
                principal_id=approver_a.id, organization_id=org.id, roles=["approver"]
            )
            memberships.create(
                principal_id=approver_b.id, organization_id=org.id, roles=["approver"]
            )
            env_id, requester_id = env.id, requester.id
            approver_a_id, approver_b_id = approver_a.id, approver_b.id

        with session_scope(factory) as session:
            operation, _, _ = service.prepare_operation(
                session,
                principal_id=requester_id,
                environment=env_id,
                workflow_id="crm.bulk_update_stage",
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
            )
            operation_id = operation.id

        outcomes: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def _decide(decided_by: str, label: str) -> None:
            # Each thread gets its own engine/session factory — a genuinely separate
            # database connection, not a shared session two threads mutate. The
            # schema is already at head (migrated once, above) — Alembic's own
            # `command.upgrade` is not safe to call concurrently from two threads
            # (it uses module-global state for its environment proxy), so threads
            # never re-migrate, only connect.
            thread_engine = create_engine_for_url(postgres_test_db_url)
            try:
                thread_factory = create_session_factory(thread_engine)
                barrier.wait(timeout=10)
                with session_scope(thread_factory) as session:
                    op = service.approve_operation(
                        session, operation_id=operation_id, decided_by=decided_by, enable_v2=True
                    )
                outcomes[label] = op.state
            finally:
                thread_engine.dispose()

        t_a = threading.Thread(target=_decide, args=(approver_a_id, "A"))
        t_b = threading.Thread(target=_decide, args=(approver_b_id, "B"))
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)

        assert set(outcomes.values()) == {"PENDING_APPROVAL", "APPROVED"}

        with session_scope(factory) as session:
            status = service.get_approval_status(
                session, operation_id=operation_id, principal_id=requester_id, enable_v2=True
            )
        assert status.ready is True
        assert {d.principal_id for d in status.decisions} == {approver_a_id, approver_b_id}
        assert status.outstanding == []
    finally:
        engine.dispose()
