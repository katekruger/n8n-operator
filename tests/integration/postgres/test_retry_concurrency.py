"""Two concurrent ``retry_operation`` calls against the same ``UNKNOWN`` parent, on
two independent database connections, with the same ``idempotency_key``, against a
real PostgreSQL instance (stage 06, AC-50) — proving the ``IntegrityError``-and-
rollback race fix in ``core.service._prepare_or_retry`` actually serializes the
namespace-unique INSERT so exactly one new operation row exists afterward and both
callers receive its ID.
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
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: retry-concurrency-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: Campaign dispatch
    description: External write.
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
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url, pool_size=5, max_overflow=5)


def test_concurrent_retries_with_the_same_idempotency_key_create_exactly_one_operation(
    postgres_test_db_url: str, tmp_path: Path
) -> None:
    engine = _migrated_engine(postgres_test_db_url)
    try:
        factory = create_session_factory(engine)

        registry_path = tmp_path / "workflows.yaml"
        registry_path.write_text(REGISTRY_YAML)
        with session_scope(factory) as session:
            PrincipalRepository(session).create(id="local", kind="local", display_name="local")
            service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
            operation, _, _ = service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id="wf.a",
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            service.execute_operation(
                session,
                operation_id=operation.id,
                handle=operation.id,
                principal_id="local",
                preflight=FakePreflight(),
            )
            service.record_execution_outcome(
                session, operation_id=operation.id, outcome="error", error={"message": "boom"}
            )
            parent_id = operation.id

        outcomes: dict[str, str] = {}
        replays: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def _retry(label: str) -> None:
            thread_engine = create_engine_for_url(postgres_test_db_url)
            try:
                thread_factory = create_session_factory(thread_engine)
                barrier.wait(timeout=10)
                with session_scope(thread_factory) as session:
                    op, replay, _token = service.retry_operation(
                        session,
                        operation_id=parent_id,
                        principal_id="local",
                        preflight=FakePreflight(),
                        server_max_argument_bytes=262_144,
                        idempotency_key="retry-race-key",
                    )
                outcomes[label] = op.id
                replays[label] = replay
            finally:
                thread_engine.dispose()

        t_a = threading.Thread(target=_retry, args=("A",))
        t_b = threading.Thread(target=_retry, args=("B",))
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)

        assert outcomes["A"] == outcomes["B"]
        assert set(replays.values()) == {True, False}

        with session_scope(factory) as session:
            children = [
                row
                for row in service.list_operations(session, principal_id="local", limit=100)
                if row.parent_operation_id == parent_id
            ]
        assert len(children) == 1
    finally:
        engine.dispose()
