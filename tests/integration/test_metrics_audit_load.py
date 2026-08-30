"""Stage 08's completion-gate load spot-check: ``get_metrics``/``list_audit_events``
stay fast against a representative operation/audit volume — not a rigorous benchmark
(that's the PR body's manual ``EXPLAIN``/``EXPLAIN ANALYZE`` pass against the real
Postgres harness, run and reported separately), just a regression guard against an
accidentally-unindexed or N+1 query path.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.storage.repository import (
    AuditLogRepository,
    ExecutionResultRepository,
    OperationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: load-test
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
""".format(hash_a="a" * 64)


@pytest.fixture
def seeded(session_factory: sessionmaker[Session], tmp_path: Any) -> sessionmaker[Session]:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        snapshot_id = service.get_active_snapshot(session).id  # type: ignore[union-attr]

        op_repo = OperationRepository(session)
        exec_repo = ExecutionResultRepository(session)
        audit_repo = AuditLogRepository(session)
        prev = audit_repo.get_last_hash()
        states = ["SUCCEEDED", "FAILED", "BLOCKED", "PENDING_APPROVAL"]
        for i in range(500):
            op_id = f"op_load_{i}"
            op_repo.create(
                id=op_id,
                principal_id="local",
                environment="default",
                snapshot_id=snapshot_id,
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state=states[i % len(states)],
                arguments={},
                argument_fingerprint=f"fp{i}",
                argument_bytes=2,
            )
            now = datetime.now(UTC)
            exec_repo.create(
                operation_id=op_id,
                status="success",
                started_at=now,
                finished_at=now,
            )
            entry = audit_repo.append(
                prev_hash=prev,
                entry_hash=f"hash{i}",
                actor="system",
                action="operation.created",
                subject_type="operation",
                subject_id=op_id,
                outcome="allowed",
            )
            prev = entry.entry_hash
    return session_factory


@pytest.mark.integration
def test_get_metrics_stays_fast_at_representative_volume(
    seeded: sessionmaker[Session],
) -> None:
    with session_scope(seeded) as session:
        started = time.monotonic()
        result = service.get_metrics(session, principal_id="local", group_by="workflow")
        elapsed = time.monotonic() - started
    assert result.totals.count == 500
    assert elapsed < 2.0, f"get_metrics took {elapsed:.3f}s against 500 operations"


@pytest.mark.integration
def test_list_audit_events_stays_fast_at_representative_volume(
    seeded: sessionmaker[Session],
) -> None:
    with session_scope(seeded) as session:
        started = time.monotonic()
        page = service.list_audit_events(session, principal_id="local", limit=100)
        elapsed = time.monotonic() - started
    assert len(page.events) == 100
    assert elapsed < 2.0, f"list_audit_events took {elapsed:.3f}s against 500 audit rows"


@pytest.mark.integration
def test_list_audit_events_full_pagination_stays_fast(seeded: sessionmaker[Session]) -> None:
    total = 0
    cursor = None
    started = time.monotonic()
    with session_scope(seeded) as session:
        while True:
            page = service.list_audit_events(
                session, principal_id="local", limit=100, cursor=cursor
            )
            total += len(page.events)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    elapsed = time.monotonic() - started
    # 500 seeded "operation.created" entries plus reload_registry's own
    # registry_snapshot entry (v1: no RBAC concept, so it is always in scope).
    assert total == 501
    assert elapsed < 5.0, f"full pagination took {elapsed:.3f}s across 500 audit rows"
