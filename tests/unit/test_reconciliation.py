"""``core.service.reconcile_operation``/``list_reconciliation_events`` (stage 06,
ADR-009/ADR-012) against a real database — exact-ID match records exactly one audit
annotation, a mismatch or lookup failure refuses and records nothing, and the
operation's own state is never touched, before or after, in any case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import ExecutionLookup, PreflightResult
from n8n_operator.errors import InstanceUnreachableError, ReconciliationNotApplicableError
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: reconciliation-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-real-1
    title: Campaign dispatch
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
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class FakeReconciliation:
    def __init__(
        self, *, lookups: dict[str, ExecutionLookup] | None = None, unreachable: bool = False
    ) -> None:
        self._lookups = lookups or {}
        self._unreachable = unreachable

    def get_execution(self, execution_id: str) -> ExecutionLookup:
        if self._unreachable:
            raise InstanceUnreachableError()
        lookup = self._lookups.get(execution_id)
        if lookup is None:
            raise InstanceUnreachableError(details={"execution_id": execution_id})
        return lookup


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def unknown_operation_id(session_factory: sessionmaker[Session], registry_path: Path) -> str:
    with session_scope(session_factory) as session:
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
        service.approve_operation(session, operation_id=operation.id, decided_by="local")
        service.execute_operation(
            session,
            operation_id=operation.id,
            handle=operation.id,
            principal_id="local",
            preflight=FakePreflight(),
        )
        service.record_execution_outcome(
            session, operation_id=operation.id, outcome="indeterminate"
        )
        return operation.id


def test_exact_id_match_records_exactly_one_annotation(
    session_factory: sessionmaker[Session], unknown_operation_id: str
) -> None:
    sink = FakeReconciliation(
        lookups={
            "exec-1": ExecutionLookup(
                execution_id="exec-1", n8n_workflow_id="n8n-real-1", status="success"
            )
        }
    )
    with session_scope(session_factory) as session:
        record = service.reconcile_operation(
            session,
            operation_id=unknown_operation_id,
            principal_id="local",
            execution_id="exec-1",
            note="confirmed via n8n UI: succeeded",
            reconciliation=sink,
        )
    assert record.execution_id == "exec-1"
    assert record.n8n_execution_status == "success"

    with session_scope(session_factory) as session:
        operation = service.get_operation(
            session, operation_id=unknown_operation_id, principal_id="local"
        )
    assert operation.state == "UNKNOWN"  # never touched

    with session_scope(session_factory) as session:
        events = service.list_reconciliation_events(
            session, operation_id=unknown_operation_id, principal_id="local"
        )
    assert len(events) == 1
    assert events[0].execution_id == "exec-1"


def test_mismatched_workflow_id_refuses_and_records_nothing(
    session_factory: sessionmaker[Session], unknown_operation_id: str
) -> None:
    sink = FakeReconciliation(
        lookups={
            "exec-wrong": ExecutionLookup(
                execution_id="exec-wrong", n8n_workflow_id="some-other-workflow", status="success"
            )
        }
    )
    with (
        session_scope(session_factory) as session,
        pytest.raises(ReconciliationNotApplicableError),
    ):
        service.reconcile_operation(
            session,
            operation_id=unknown_operation_id,
            principal_id="local",
            execution_id="exec-wrong",
            note="attempted",
            reconciliation=sink,
        )

    with session_scope(session_factory) as session:
        events = service.list_reconciliation_events(
            session, operation_id=unknown_operation_id, principal_id="local"
        )
    assert events == []


def test_a_non_unknown_operation_refuses(
    session_factory: sessionmaker[Session], registry_path: Path
) -> None:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local2", kind="local", display_name="local2")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        operation, _, _ = service.prepare_operation(
            session,
            principal_id="local2",
            environment="default",
            workflow_id="wf.a",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
        )
        pending_id = operation.id

    with (
        session_scope(session_factory) as session,
        pytest.raises(ReconciliationNotApplicableError),
    ):
        service.reconcile_operation(
            session,
            operation_id=pending_id,
            principal_id="local2",
            execution_id="exec-1",
            note="n/a",
            reconciliation=FakeReconciliation(),
        )


def test_unreachable_instance_refuses_and_records_nothing(
    session_factory: sessionmaker[Session], unknown_operation_id: str
) -> None:
    with (
        session_scope(session_factory) as session,
        pytest.raises(ReconciliationNotApplicableError),
    ):
        service.reconcile_operation(
            session,
            operation_id=unknown_operation_id,
            principal_id="local",
            execution_id="exec-missing",
            note="attempted",
            reconciliation=FakeReconciliation(unreachable=True),
        )

    with session_scope(session_factory) as session:
        events = service.list_reconciliation_events(
            session, operation_id=unknown_operation_id, principal_id="local"
        )
    assert events == []


def test_list_reconciliation_events_returns_entries_in_order(
    session_factory: sessionmaker[Session], unknown_operation_id: str
) -> None:
    sink = FakeReconciliation(
        lookups={
            "exec-1": ExecutionLookup(
                execution_id="exec-1", n8n_workflow_id="n8n-real-1", status="success"
            )
        }
    )
    with session_scope(session_factory) as session:
        service.reconcile_operation(
            session,
            operation_id=unknown_operation_id,
            principal_id="local",
            execution_id="exec-1",
            note="first",
            reconciliation=sink,
        )
    with session_scope(session_factory) as session:
        events = service.list_reconciliation_events(
            session, operation_id=unknown_operation_id, principal_id="local"
        )
    assert [e.note for e in events] == ["first"]
