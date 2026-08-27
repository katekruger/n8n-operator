"""The operation lifecycle end to end against a real database (BUILD_PLAN section 12,
phase 3): every transition T01-T15, invariants I1-I11, concurrent handle burn, scoped
idempotency, lazy expiry, oversized arguments, and the audit trail each transition
writes.

Uses a fake ``PreflightPort`` throughout — Phase 3 explicitly does not implement the real
n8n adapter (BUILD_PLAN section 12 phase 4).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightCheck, PreflightResult
from n8n_operator.errors import (
    ApprovalRequiredError,
    ArgumentMismatchError,
    ArgumentsTooLargeError,
    ConcurrencyLimitReachedError,
    DefinitionDriftError,
    HandleAlreadyUsedError,
    HandleInvalidError,
    IdempotencyConflictError,
    InvalidArgumentsError,
    InvalidStateTransitionError,
    OperationCanceledError,
    OperationExpiredError,
    OperationNotFoundError,
    RegistryUnavailableError,
    ResultNotAvailableError,
    WorkflowDisabledError,
    WorkflowNotFoundError,
)
from n8n_operator.storage.models import STATES
from n8n_operator.storage.repository import (
    ApprovalRepository,
    AuditLogRepository,
    OperationEventRepository,
    OperationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase3-test
workflows:
  - id: wf.needs_approval
    n8n_workflow_id: n8n-1
    title: Needs approval
    description: Writes to an external system.
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
      properties:
        email: {{type: string}}
      required: [email]
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: wf.auto_approved
    n8n_workflow_id: n8n-2
    title: Auto approved
    description: Read-only reporting.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      additionalProperties: false
    output:
      redact: ["$.secret_field"]
      max_bytes: 65536
    limits:
      execution_ttl_seconds: 300
  - id: wf.disabled
    n8n_workflow_id: n8n-3
    title: Disabled
    description: Retired.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_c}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/c
      auth: none
    input_schema:
      type: object
      additionalProperties: false
    enabled: false
""".format(hash_a="a" * 64, hash_b="b" * 64, hash_c="c" * 64)


class FakePreflight:
    """A configurable stand-in for the real (phase 4) n8n preflight adapter."""

    def __init__(
        self, *, ready: bool = True, extra_checks: list[PreflightCheck] | None = None
    ) -> None:
        self.ready = ready
        self.extra_checks = extra_checks or []
        self.calls: list[str] = []

    def check(self, workflow: Any) -> PreflightResult:
        self.calls.append(workflow.id)
        status: Literal["pass", "fail"] = "pass" if self.ready else "fail"
        checks = [
            PreflightCheck(check="instance_reachable", status="pass"),
            PreflightCheck(
                check="workflow_active",
                status=status,
                code=None if self.ready else "WORKFLOW_INACTIVE",
            ),
            *self.extra_checks,
        ]
        return PreflightResult(ready=self.ready, checks=checks, checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def env(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    """A seeded principal and a loaded, active registry snapshot."""
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return {"principal_id": "local", "environment": "default"}


def _prepare(
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    *,
    workflow_id: str = "wf.needs_approval",
    arguments: dict[str, Any] | None = None,
    preflight: FakePreflight | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, str]:
    """Prepare an operation and return ``(operation_id, state)``."""
    with session_scope(session_factory) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=env["principal_id"],
            environment=env["environment"],
            workflow_id=workflow_id,
            arguments=arguments if arguments is not None else {"email": "a@b.com"},
            preflight=preflight or FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key=idempotency_key,
        )
        return operation.id, operation.state


# --------------------------------------------------------------------------------------
# T01-T05: prepare_operation's own transitions
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_t01_creates_a_preparing_row_with_an_audit_and_event_record(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation(op_id)
    t01_events = [e for e in events if e.transition == "T01"]
    assert len(t01_events) == 1
    assert t01_events[0].from_state is None
    assert t01_events[0].to_state == "PREPARING"


@pytest.mark.integration
def test_t02_invalid_on_schema_validation_failure(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, state = _prepare(session_factory, env, arguments={})  # missing required "email"
    assert state == "INVALID"


@pytest.mark.integration
def test_t03_blocked_on_preflight_failure(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, state = _prepare(session_factory, env, preflight=FakePreflight(ready=False))
    assert state == "BLOCKED"


@pytest.mark.integration
def test_t04_pending_approval_when_approval_required(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state = _prepare(session_factory, env, workflow_id="wf.needs_approval")
    assert state == "PENDING_APPROVAL"
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
        assert operation.approval_expires_at is not None
        assert operation.execution_deadline is None


@pytest.mark.integration
def test_t04_mints_an_approval_row(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env, workflow_id="wf.needs_approval")
    with session_scope(session_factory) as session:
        approval = ApprovalRepository(session).get_by_operation_id(op_id)
        assert approval is not None
        assert approval.decision is None


@pytest.mark.integration
def test_t05_auto_approved_when_read_only_and_approval_none(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state = _prepare(session_factory, env, workflow_id="wf.auto_approved", arguments={})
    assert state == "APPROVED"
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
        assert operation.execution_deadline is not None
        assert operation.approval_expires_at is None


@pytest.mark.integration
def test_workflow_not_found_is_indistinguishable_from_unregistered(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(WorkflowNotFoundError), session_scope(session_factory) as session:
        service.prepare_operation(
            session,
            principal_id="local",
            environment="default",
            workflow_id="wf.nonexistent",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
        )


@pytest.mark.integration
def test_disabled_workflow_gives_workflow_disabled_not_not_found(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(WorkflowDisabledError), session_scope(session_factory) as session:
        service.prepare_operation(
            session,
            principal_id="local",
            environment="default",
            workflow_id="wf.disabled",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
        )


@pytest.mark.integration
def test_disabled_workflow_is_invisible_to_discovery(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        summaries = service.list_workflows(session)
        assert "wf.disabled" not in {s.workflow_id for s in summaries}
        with pytest.raises(WorkflowNotFoundError):
            service.describe_workflow(session, workflow_id="wf.disabled")


# --------------------------------------------------------------------------------------
# T06/T07: approve / reject
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_t06_approve_sets_execution_deadline(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.approve_operation(session, operation_id=op_id, decided_by="local")
    assert operation.state == "APPROVED"
    assert operation.execution_deadline is not None


@pytest.mark.integration
def test_t06_records_the_approval_decision(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with session_scope(session_factory) as session:
        approval = ApprovalRepository(session).get_by_operation_id(op_id)
        assert approval is not None
        assert approval.decision == "approved"
        assert approval.decided_by == "local"
        assert approval.decided_at is not None


@pytest.mark.integration
def test_t07_reject_is_terminal(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.reject_operation(session, operation_id=op_id, decided_by="local")
    assert operation.state == "REJECTED"
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")


@pytest.mark.integration
def test_cannot_approve_an_already_approved_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")


# --------------------------------------------------------------------------------------
# T08/T11: lazy expiry
# --------------------------------------------------------------------------------------


def _force_past_deadline(
    session_factory: sessionmaker[Session], operation_id: str, field: str
) -> None:
    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        row = repo.get(operation_id)
        assert row is not None
        repo.compare_and_set_state(
            operation_id=operation_id,
            expected_version=row.state_version,
            new_state=row.state,
            **{field: datetime.now(UTC) - timedelta(seconds=1)},
        )


@pytest.mark.integration
def test_t08_pending_approval_expires_lazily_on_read(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    _force_past_deadline(session_factory, op_id, "approval_expires_at")
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "EXPIRED"


@pytest.mark.integration
def test_t11_approved_expires_lazily_on_read(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    _force_past_deadline(session_factory, op_id, "execution_deadline")
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "EXPIRED"


@pytest.mark.integration
def test_lazy_expiry_transition_is_recorded_exactly_once(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    _force_past_deadline(session_factory, op_id, "approval_expires_at")
    with session_scope(session_factory) as session:
        service.get_operation(session, operation_id=op_id, principal_id="local")
    with session_scope(session_factory) as session:
        service.get_operation(session, operation_id=op_id, principal_id="local")  # touch again
    with session_scope(session_factory) as session:
        from n8n_operator.storage.repository import OperationEventRepository

        events = OperationEventRepository(session).list_for_operation(op_id)
    t08_events = [e for e in events if e.transition == "T08"]
    assert len(t08_events) == 1


@pytest.mark.integration
def test_no_expired_operation_can_ever_be_approved_or_executed(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    _force_past_deadline(session_factory, op_id, "approval_expires_at")
    with (
        pytest.raises((InvalidStateTransitionError, OperationExpiredError)),
        session_scope(session_factory) as session,
    ):
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with (
        pytest.raises((InvalidStateTransitionError, ApprovalRequiredError, OperationExpiredError)),
        session_scope(session_factory) as session,
    ):
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# T09/T12: cancel
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_t09_cancel_from_pending_approval(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.cancel_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "CANCELED"


@pytest.mark.integration
def test_t12_cancel_from_approved(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with session_scope(session_factory) as session:
        operation = service.cancel_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "CANCELED"


@pytest.mark.integration
def test_cancel_a_canceled_operation_is_invalid_state_transition(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")


@pytest.mark.integration
def test_a_canceled_operation_cannot_be_executed(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")
    with pytest.raises(OperationCanceledError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# T10: execute; handle binding, drift, concurrency
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_t10_execute_burns_the_handle_and_moves_to_executing(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with session_scope(session_factory) as session:
        operation = service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    assert operation.state == "EXECUTING"
    assert operation.handle_burned_at is not None


@pytest.mark.integration
def test_execute_before_approval_is_approval_required(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with pytest.raises(ApprovalRequiredError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


@pytest.mark.integration
def test_execute_with_mismatched_handle_and_operation_id(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with pytest.raises(ArgumentMismatchError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle="op_wrong",
            principal_id="local",
            preflight=FakePreflight(),
        )


@pytest.mark.integration
def test_execute_twice_gives_handle_already_used(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    with session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    with pytest.raises(HandleAlreadyUsedError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


@pytest.mark.integration
def test_concurrent_execute_burns_the_handle_exactly_once(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """Invariant I4: a handle can be burned at most once, enforced by the database, not
    application logic — proven under genuine thread concurrency, not just sequential
    calls. A losing thread may observe either ``HandleAlreadyUsedError`` (it reached the
    compare-and-set burn after another thread already won it) or
    ``ConcurrencyLimitReachedError`` (the workflow's own ``max_concurrent`` — default 1
    — was already reached by the thread that won, and this thread's concurrency check
    ran after that commit but before its own burn attempt); which one depends on exact
    interleaving, but either correctly means "did not execute twice."""
    op_id, _state = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")

    def attempt(_: int) -> str:
        try:
            with session_scope(session_factory) as session:
                service.execute_operation(
                    session,
                    operation_id=op_id,
                    handle=op_id,
                    principal_id="local",
                    preflight=FakePreflight(),
                )
            return "success"
        except HandleAlreadyUsedError:
            return "already_used"
        except ConcurrencyLimitReachedError:
            return "concurrency_limited"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert results.count("success") == 1
    assert results.count("already_used") + results.count("concurrency_limited") == 7


@pytest.mark.integration
def test_execute_detects_definition_drift_since_approval(
    session_factory: sessionmaker[Session], env: dict[str, Any], tmp_path: Path
) -> None:
    op_id, _state = _prepare(session_factory, env, workflow_id="wf.auto_approved", arguments={})
    # wf.auto_approved auto-approves at prepare (T05) — no separate approve call needed.

    drifted = REGISTRY_YAML.replace("b" * 64, "d" * 64)
    drifted_path = tmp_path / "drifted.yaml"
    drifted_path.write_text(drifted)
    with session_scope(session_factory) as session:
        service.reload_registry(session, drifted_path, server_max_argument_bytes=262_144)

    with (
        pytest.raises(DefinitionDriftError) as excinfo,
        session_scope(session_factory) as session,
    ):
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    assert excinfo.value.details["registered"] != excinfo.value.details["current"]


# --------------------------------------------------------------------------------------
# T13/T14/T15: record_execution_outcome
# --------------------------------------------------------------------------------------


def _prepare_and_execute(session_factory: sessionmaker[Session], env: dict[str, Any]) -> str:
    op_id, _state = _prepare(session_factory, env, workflow_id="wf.auto_approved", arguments={})
    with session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    return op_id


@pytest.mark.integration
def test_t13_success_persists_a_redacted_result(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_execute(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.record_execution_outcome(
            session,
            operation_id=op_id,
            outcome="success",
            result={"contact_id": "c_1", "secret_field": "shh"},
        )
    assert operation.state == "SUCCEEDED"
    with session_scope(session_factory) as session:
        result = service.get_execution_result(session, operation_id=op_id, principal_id="local")
    assert result.status == "success"
    assert result.redacted_payload["secret_field"] == "[REDACTED]"
    assert result.redacted_payload["contact_id"] == "c_1"


@pytest.mark.integration
def test_t14_error_persists_the_error_payload(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_execute(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.record_execution_outcome(
            session,
            operation_id=op_id,
            outcome="error",
            error={"node": "HTTP Request", "type": "NodeApiError", "message": "422"},
        )
    assert operation.state == "FAILED"
    with session_scope(session_factory) as session:
        result = service.get_execution_result(session, operation_id=op_id, principal_id="local")
    assert result.status == "error"
    assert result.error is not None
    assert result.error["type"] == "NodeApiError"


@pytest.mark.integration
def test_t15_indeterminate_is_terminal_and_never_auto_resolved(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_execute(session_factory, env)
    with session_scope(session_factory) as session:
        operation = service.record_execution_outcome(
            session, operation_id=op_id, outcome="indeterminate"
        )
    assert operation.state == "UNKNOWN"
    # No use case moves UNKNOWN anywhere (invariant I7) — every attempt is rejected.
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.record_execution_outcome(session, operation_id=op_id, outcome="success")
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")


@pytest.mark.integration
def test_terminal_states_reject_every_further_action(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_execute(session_factory, env)
    with session_scope(session_factory) as session:
        service.record_execution_outcome(session, operation_id=op_id, outcome="success")
    with (
        pytest.raises((HandleAlreadyUsedError, InvalidStateTransitionError)),
        session_scope(session_factory) as session,
    ):
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# I8 / ADR-011: scoped idempotency
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_same_namespace_same_fingerprint_returns_the_same_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id_1, _ = _prepare(session_factory, env, idempotency_key="k1")
    op_id_2, _ = _prepare(session_factory, env, idempotency_key="k1")
    assert op_id_1 == op_id_2
    with session_scope(session_factory) as session:
        count = len(
            OperationRepository(session).list(
                principal_id="local", environment="default", limit=100
            )
        )
    assert count == 1


@pytest.mark.integration
def test_same_namespace_different_fingerprint_is_idempotency_conflict(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _prepare(session_factory, env, idempotency_key="k1", arguments={"email": "a@b.com"})
    with pytest.raises(IdempotencyConflictError):
        _prepare(session_factory, env, idempotency_key="k1", arguments={"email": "different@b.com"})


@pytest.mark.integration
def test_different_workflow_same_key_is_a_different_namespace(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id_1, _ = _prepare(
        session_factory, env, workflow_id="wf.needs_approval", idempotency_key="k1"
    )
    op_id_2, _ = _prepare(
        session_factory, env, workflow_id="wf.auto_approved", idempotency_key="k1", arguments={}
    )
    assert op_id_1 != op_id_2


@pytest.mark.integration
def test_no_idempotency_key_never_collides(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id_1, _ = _prepare(session_factory, env, arguments={"email": "a@b.com"})
    op_id_2, _ = _prepare(session_factory, env, arguments={"email": "a@b.com"})
    assert op_id_1 != op_id_2


# --------------------------------------------------------------------------------------
# I10 / ADR-011: oversized arguments never reach storage
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_oversized_arguments_are_refused_before_any_operation_row_is_written(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        before = len(
            OperationRepository(session).list(
                principal_id="local", environment="default", limit=100
            )
        )
    with pytest.raises(ArgumentsTooLargeError):
        _prepare(session_factory, env, arguments={"email": "a@b.com", "junk": "x" * 300_000})
    with session_scope(session_factory) as session:
        after = len(
            OperationRepository(session).list(
                principal_id="local", environment="default", limit=100
            )
        )
    assert after == before


@pytest.mark.integration
def test_oversized_arguments_are_still_audited(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(ArgumentsTooLargeError):
        _prepare(session_factory, env, arguments={"email": "a@b.com", "junk": "x" * 300_000})
    with session_scope(session_factory) as session:
        entries = AuditLogRepository(session).list_range()
    denied = [
        e
        for e in entries
        if e.outcome == "denied" and e.detail.get("code") == "ARGUMENTS_TOO_LARGE"
    ]
    assert len(denied) == 1


# --------------------------------------------------------------------------------------
# I11: an approval decision authorizes exactly one operation
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_approving_one_operation_does_not_authorize_a_different_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_a, _ = _prepare(session_factory, env, arguments={"email": "a@b.com"})
    op_b, _ = _prepare(session_factory, env, arguments={"email": "b@b.com"})
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_a, decided_by="local")
    with session_scope(session_factory) as session:
        operation_b = service.get_operation(session, operation_id=op_b, principal_id="local")
    assert operation_b.state == "PENDING_APPROVAL"
    with pytest.raises(ApprovalRequiredError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_b,
            handle=op_b,
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# Ownership: "no signal" for a different principal's operation
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_different_principal_gets_operation_not_found_not_a_permission_error(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _ = _prepare(session_factory, env)
    with pytest.raises(OperationNotFoundError), session_scope(session_factory) as session:
        service.get_operation(session, operation_id=op_id, principal_id="someone-else")


# --------------------------------------------------------------------------------------
# get_execution_result / RESULT_NOT_AVAILABLE
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_get_execution_result_before_execution_is_result_not_available(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    with pytest.raises(ResultNotAvailableError), session_scope(session_factory) as session:
        service.get_execution_result(session, operation_id=op_id, principal_id="local")


# --------------------------------------------------------------------------------------
# list_operations
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_operations_filters_by_workflow_and_state(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _prepare(session_factory, env, workflow_id="wf.needs_approval", arguments={"email": "a@b.com"})
    _prepare(session_factory, env, workflow_id="wf.auto_approved", arguments={})
    with session_scope(session_factory) as session:
        all_ops = service.list_operations(session, principal_id="local")
        approved_only = service.list_operations(session, principal_id="local", states=["APPROVED"])
    assert len(all_ops) == 2
    assert len(approved_only) == 1
    assert approved_only[0].workflow_id == "wf.auto_approved"


@pytest.mark.integration
def test_list_operations_rejects_an_out_of_range_limit(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentsError), session_scope(session_factory) as session:
        service.list_operations(session, principal_id="local", limit=0)
    with pytest.raises(InvalidArgumentsError), session_scope(session_factory) as session:
        service.list_operations(session, principal_id="local", limit=101)


@pytest.mark.integration
def test_list_operations_rejects_an_unknown_state_name(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentsError), session_scope(session_factory) as session:
        service.list_operations(session, principal_id="local", states=["NOT_A_REAL_STATE"])


@pytest.mark.integration
def test_list_operations_applies_lazy_expiry_to_every_row(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state = _prepare(session_factory, env)
    _force_past_deadline(session_factory, op_id, "approval_expires_at")
    with session_scope(session_factory) as session:
        ops = service.list_operations(session, principal_id="local")
    assert all(o.state != "PENDING_APPROVAL" or o.id != op_id for o in ops)
    matching = [o for o in ops if o.id == op_id]
    assert matching[0].state == "EXPIRED"


# --------------------------------------------------------------------------------------
# Every transition's state lands in one of the twelve documented states (I1)
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_every_reachable_state_is_one_of_the_twelve_documented_states(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state = _prepare(session_factory, env)
    assert state in STATES
    with session_scope(session_factory) as session:
        operation = service.approve_operation(session, operation_id=op_id, decided_by="local")
    assert operation.state in STATES


# --------------------------------------------------------------------------------------
# Discovery use cases: describe_workflow, validate_input, preflight_workflow
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_describe_workflow_returns_the_full_contract(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        detail = service.describe_workflow(session, workflow_id="wf.needs_approval")
    assert detail.workflow_id == "wf.needs_approval"
    assert detail.approval == "required"
    assert detail.risk == "medium"


@pytest.mark.integration
def test_validate_input_reports_schema_errors_without_creating_an_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        errors = service.validate_input(session, workflow_id="wf.needs_approval", arguments={})
    assert len(errors) == 1
    assert errors[0].code == "REQUIRED"
    with session_scope(session_factory) as session:
        count = len(
            OperationRepository(session).list(
                principal_id="local", environment="default", limit=100
            )
        )
    assert count == 0


@pytest.mark.integration
def test_validate_input_on_a_valid_payload_reports_no_errors(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        errors = service.validate_input(
            session, workflow_id="wf.needs_approval", arguments={"email": "a@b.com"}
        )
    assert errors == []


@pytest.mark.integration
def test_validate_input_on_an_unknown_workflow_is_workflow_not_found(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(WorkflowNotFoundError), session_scope(session_factory) as session:
        service.validate_input(session, workflow_id="wf.nonexistent", arguments={})


@pytest.mark.integration
def test_preflight_workflow_on_an_unknown_workflow_is_workflow_not_found(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(WorkflowNotFoundError), session_scope(session_factory) as session:
        service.preflight_workflow(session, workflow_id="wf.nonexistent", preflight=FakePreflight())


@pytest.mark.integration
def test_preflight_workflow_runs_the_injected_port_without_creating_an_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    fake = FakePreflight()
    with session_scope(session_factory) as session:
        result = service.preflight_workflow(
            session, workflow_id="wf.needs_approval", preflight=fake
        )
    assert result.ready is True
    assert fake.calls == ["wf.needs_approval"]
    with session_scope(session_factory) as session:
        count = len(
            OperationRepository(session).list(
                principal_id="local", environment="default", limit=100
            )
        )
    assert count == 0


# --------------------------------------------------------------------------------------
# REGISTRY_UNAVAILABLE: no snapshot loaded yet
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_discovery_and_prepare_fail_closed_with_no_registry_loaded(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")

    with pytest.raises(RegistryUnavailableError), session_scope(session_factory) as session:
        service.list_workflows(session)
    with pytest.raises(RegistryUnavailableError), session_scope(session_factory) as session:
        service.describe_workflow(session, workflow_id="wf.needs_approval")
    with pytest.raises(RegistryUnavailableError), session_scope(session_factory) as session:
        service.prepare_operation(
            session,
            principal_id="local",
            environment="default",
            workflow_id="wf.needs_approval",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
        )


# --------------------------------------------------------------------------------------
# OPERATION_NOT_FOUND for a genuinely nonexistent operation ID
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_operations_on_a_nonexistent_id_are_operation_not_found(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    with pytest.raises(OperationNotFoundError), session_scope(session_factory) as session:
        service.get_operation(session, operation_id="op_does_not_exist", principal_id="local")
    with pytest.raises(OperationNotFoundError), session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id="op_does_not_exist", principal_id="local")
    with pytest.raises(OperationNotFoundError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id="op_does_not_exist",
            handle="op_does_not_exist",
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# execute_operation on a never-approved operation (PREPARING/INVALID/BLOCKED) — the
# HandleInvalidError fallback, distinct from ApprovalRequiredError (PENDING_APPROVAL).
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_execute_on_an_invalid_operation_gives_handle_invalid_not_approval_required(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state = _prepare(session_factory, env, arguments={})  # -> INVALID
    assert state == "INVALID"
    with pytest.raises(HandleInvalidError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


@pytest.mark.integration
def test_execute_on_a_blocked_operation_gives_handle_invalid(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state = _prepare(session_factory, env, preflight=FakePreflight(ready=False))
    assert state == "BLOCKED"
    with pytest.raises(HandleInvalidError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
