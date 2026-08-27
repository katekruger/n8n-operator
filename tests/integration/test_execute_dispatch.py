"""Phase 7: execution, the highest-risk boundary — ``core.service.execute_operation``'s
extended verification chain and ``core.service.dispatch_operation``'s exactly-once
dispatch and conservative outcome mapping (BUILD_PLAN section 12, phase 7).

Uses a fake ``DispatchPort`` throughout, exactly as Phase 3's operation-lifecycle suite
uses a fake ``PreflightPort`` — no network in the loop, but the same seam Phase 4's real
``n8n.dispatch.N8nDispatch`` satisfies in production.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import DispatchOutcome, PreflightCheck, PreflightResult
from n8n_operator.errors import (
    ArgumentMismatchError,
    ConcurrencyLimitReachedError,
    DefinitionDriftError,
    HandleAlreadyUsedError,
    InvalidStateTransitionError,
    RateLimitedError,
)
from n8n_operator.storage.repository import AuditLogRepository, PrincipalRepository
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase7-test
workflows:
  - id: wf.dispatch
    n8n_workflow_id: n8n-1
    title: Dispatch me
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
      correlation: response_envelope
    input_schema:
      type: object
      properties:
        email: {{type: string}}
      required: [email]
      additionalProperties: false
    output:
      redact: ["$.secret_field"]
      max_bytes: 65536
      include_node_trace: true
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
      max_concurrent: 1
      rate_limit_per_minute: 100
  - id: wf.auto
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
    limits:
      execution_ttl_seconds: 300
  - id: wf.tiny_output
    n8n_workflow_id: n8n-3
    title: Tiny output cap
    description: Truncation test target.
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
    output:
      max_bytes: 40
    limits:
      execution_ttl_seconds: 300
  - id: wf.rate_limited
    n8n_workflow_id: n8n-4
    title: Rate limited
    description: Rate-limit test target.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_d}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/d
      auth: none
    input_schema:
      type: object
      additionalProperties: false
    limits:
      execution_ttl_seconds: 300
      rate_limit_per_minute: 1
""".format(hash_a="a" * 64, hash_b="b" * 64, hash_c="c" * 64, hash_d="d" * 64)


class FakePreflight:
    """A configurable stand-in for the real (phase 4) n8n preflight adapter.

    ``fail_check`` names one specific check to report ``fail`` for (default
    ``instance_reachable``, when ``ready=False``) — lets tests target
    :func:`service._verify_live_before_execute`'s specific-error mapping rather than
    only ever exercising its ``ready=True`` fast path.
    """

    def __init__(self, *, ready: bool = True, fail_check: str | None = None) -> None:
        self.ready = ready
        self.fail_check = fail_check or "instance_reachable"
        self.calls: list[str] = []

    def check(self, workflow: Any) -> PreflightResult:
        self.calls.append(workflow.id)
        if self.ready:
            checks = [
                PreflightCheck(check="instance_reachable", status="pass"),
                PreflightCheck(check="workflow_exists", status="pass"),
                PreflightCheck(check="workflow_active", status="pass"),
                PreflightCheck(check="definition_unchanged", status="pass"),
            ]
            return PreflightResult(ready=True, checks=checks, checked_at=datetime.now(UTC))
        names = ["instance_reachable", "workflow_exists", "workflow_active", "definition_unchanged"]
        checks = []
        failed_yet = False
        for name in names:
            if failed_yet:
                checks.append(PreflightCheck(check=name, status="skipped"))
            elif name == self.fail_check:
                checks.append(PreflightCheck(check=name, status="fail", detail={"reason": "test"}))
                failed_yet = True
            else:
                checks.append(PreflightCheck(check=name, status="pass"))
        return PreflightResult(ready=False, checks=checks, checked_at=datetime.now(UTC))


class FakeDispatch:
    """A configurable stand-in for the real (phase 7) ``n8n.dispatch.N8nDispatch``
    adapter. ``outcome`` is fixed per instance — enough for every test here, since none
    needs a *sequence* of different outcomes across calls (ADR-005: there is never a
    second dispatch call to vary the outcome of)."""

    def __init__(
        self,
        *,
        outcome: DispatchOutcome | None = None,
        node_trace: dict[str, Any] | None = None,
    ) -> None:
        self.outcome = outcome or DispatchOutcome(
            kind="success",
            http_status=200,
            result={"ok": True},
            execution_id="exec-1",
            correlation_available=True,
        )
        self.node_trace = node_trace
        self.dispatch_calls = 0
        self.fetch_node_trace_calls = 0

    def dispatch(
        self, workflow: Any, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        self.dispatch_calls += 1
        return self.outcome

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        self.fetch_node_trace_calls += 1
        return self.node_trace


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def env(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return {"principal_id": "local", "environment": "default"}


def _prepare(
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    *,
    workflow_id: str = "wf.dispatch",
    arguments: dict[str, Any] | None = None,
    preflight: FakePreflight | None = None,
) -> str:
    with session_scope(session_factory) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=env["principal_id"],
            environment=env["environment"],
            workflow_id=workflow_id,
            arguments=arguments if arguments is not None else {"email": "a@b.com"},
            preflight=preflight or FakePreflight(),
            server_max_argument_bytes=262_144,
        )
        return operation.id


def _prepare_and_approve(
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    *,
    workflow_id: str = "wf.dispatch",
    arguments: dict[str, Any] | None = None,
) -> str:
    op_id = _prepare(session_factory, env, workflow_id=workflow_id, arguments=arguments)
    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id, decided_by="local")
    return op_id


def _burn(
    session_factory: sessionmaker[Session],
    op_id: str,
    *,
    preflight: FakePreflight | None = None,
) -> None:
    with session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=preflight or FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# AC-10 — dispatches exactly once; a reused handle is refused and dispatches nothing.
# --------------------------------------------------------------------------------------


def test_ac10_dispatches_exactly_once_and_a_reused_handle_dispatches_nothing(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    dispatch = FakeDispatch()
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "SUCCEEDED"
    assert dispatch.dispatch_calls == 1

    with pytest.raises(HandleAlreadyUsedError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )
    assert dispatch.dispatch_calls == 1  # the refused re-attempt never reached dispatch


# --------------------------------------------------------------------------------------
# AC-13 — semantic drift after approval, caught two ways: the registry's own snapshot
# (already covered in test_core_service_operations.py) and a *live* n8n re-check.
# --------------------------------------------------------------------------------------


def test_ac13_live_drift_since_approval_refuses_execution_and_dispatches_nothing(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    drifted_preflight = FakePreflight(ready=False, fail_check="definition_unchanged")
    with pytest.raises(DefinitionDriftError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=drifted_preflight,
        )
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "APPROVED"  # refused before the burn; nothing changed
    assert operation.handle_burned_at is None


@pytest.mark.parametrize(
    ("fail_check", "expected_error"),
    [
        ("instance_reachable", "InstanceUnreachableError"),
        ("workflow_exists", "WorkflowMissingOnInstanceError"),
        ("workflow_active", "WorkflowInactiveError"),
    ],
)
def test_live_preflight_failures_map_to_specific_execute_time_errors(
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    fail_check: str,
    expected_error: str,
) -> None:
    from n8n_operator import errors

    op_id = _prepare_and_approve(session_factory, env)
    error_cls = getattr(errors, expected_error)
    with pytest.raises(error_cls), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(ready=False, fail_check=fail_check),
        )


# --------------------------------------------------------------------------------------
# AC-14 — read_only + approval:none dispatches without human interaction.
# --------------------------------------------------------------------------------------


def test_ac14_auto_approved_workflow_dispatches_without_human_interaction(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare(session_factory, env, workflow_id="wf.auto", arguments={})
    with session_scope(session_factory) as session:
        prepared = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert prepared.state == "APPROVED"  # T05, no approve_operation call needed

    _burn(session_factory, op_id)
    dispatch = FakeDispatch()
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "SUCCEEDED"
    assert dispatch.dispatch_calls == 1


# --------------------------------------------------------------------------------------
# AC-15 — a workflow that errors in n8n leaves the operation FAILED, and the node trace
# names the failing node and its error message.
# --------------------------------------------------------------------------------------


def test_ac15_n8n_error_leaves_operation_failed_with_a_named_failing_node(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    node_trace = {
        "nodes": [
            {
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "status": "success",
                "duration_ms": 3,
            },
            {
                "name": "HTTP Request",
                "type": "n8n-nodes-base.httpRequest",
                "status": "error",
                "duration_ms": 812,
            },
        ],
        "failed_node": "HTTP Request",
        "failed_node_error": "Request failed with status 422",
    }
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="error",
            http_status=422,
            result={"message": "Request failed with status 422"},
            execution_id="exec-2",
            correlation_available=True,
        ),
        node_trace=node_trace,
    )
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "FAILED"
    with session_scope(session_factory) as session:
        result = service.get_execution_result(session, operation_id=op_id, principal_id="local")
    assert result.status == "error"
    assert result.node_trace is not None
    assert result.node_trace["failed_node"] == "HTTP Request"
    assert result.node_trace["failed_node_error"] == "Request failed with status 422"


# --------------------------------------------------------------------------------------
# AC-16 — a dispatch that times out leaves the operation UNKNOWN, dispatches nothing
# further, and the audit log records the indeterminacy exactly once. Covers both
# "timeout after n8n received the request" and "timeout before any response arrived" —
# ADR-009 treats them identically: a lost response is a lost response.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["timeout_before_any_response", "timeout_after_n8n_received_the_request", "lost_response"],
)
def test_ac16_indeterminate_dispatch_leaves_operation_unknown(
    session_factory: sessionmaker[Session], env: dict[str, Any], scenario: str
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    # All three scenarios reach `core.service` identically: DispatchPort.dispatch has
    # no way to distinguish "n8n never saw the request" from "n8n started running it"
    # from "a response arrived but was lost in transit" — ADR-009 deliberately does not
    # ask it to. Whatever n8n did or didn't do, `core.service` records exactly the same
    # thing: UNKNOWN, no execution ID, no further action.
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="indeterminate",
            http_status=None,
            result=None,
            execution_id=None,
            correlation_available=False,
        )
    )
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "UNKNOWN"
    assert operation.n8n_execution_id is None
    assert dispatch.dispatch_calls == 1

    # No code path transitions UNKNOWN to anything (invariant I7).
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.record_execution_outcome(session, operation_id=op_id, outcome="success")
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")

    with session_scope(session_factory) as session:
        entries = AuditLogRepository(session).list_range()
    indeterminate_events = [
        e for e in entries if e.subject_id == op_id and e.action == "operation.indeterminate"
    ]
    assert len(indeterminate_events) == 1


# --------------------------------------------------------------------------------------
# Invalid/absent correlation envelope: does not, by itself, demote a real 2xx response
# to indeterminate (ADR-009 section 2) — and a node trace is never fetched when there
# is no trustworthy execution ID to fetch it for.
# --------------------------------------------------------------------------------------


def test_missing_correlation_does_not_block_success_and_skips_node_trace_fetch(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="success",
            http_status=200,
            result={"ok": True},
            execution_id=None,
            correlation_available=False,
        ),
        node_trace={"nodes": [], "failed_node": None, "failed_node_error": None},
    )
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "SUCCEEDED"
    assert operation.n8n_execution_id is None
    assert dispatch.fetch_node_trace_calls == 0  # never guessed which execution this was

    with session_scope(session_factory) as session:
        result = service.get_execution_result(session, operation_id=op_id, principal_id="local")
    assert result.node_trace is None


# --------------------------------------------------------------------------------------
# AC-19 — configured output.redact paths are absent from the persisted, then read-back,
# result.
# --------------------------------------------------------------------------------------


def test_ac19_redact_paths_are_absent_from_the_persisted_result(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="success",
            http_status=200,
            result={"contact_id": "c_1", "secret_field": "shh"},
            execution_id="exec-3",
            correlation_available=True,
        )
    )
    service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    with session_scope(session_factory) as session:
        result = service.get_execution_result(session, operation_id=op_id, principal_id="local")
    assert result.redacted_payload["contact_id"] == "c_1"
    assert result.redacted_payload["secret_field"] == "[REDACTED]"


# --------------------------------------------------------------------------------------
# Result truncation: an oversized payload is capped, and the truncation is reported.
# --------------------------------------------------------------------------------------


def test_oversized_result_is_truncated_and_reported(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare(session_factory, env, workflow_id="wf.tiny_output", arguments={})
    _burn(session_factory, op_id)
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind="success",
            http_status=200,
            result={"payload": "x" * 500},
            execution_id=None,
            correlation_available=False,
        )
    )
    operation = service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert operation.state == "SUCCEEDED"
    with session_scope(session_factory) as session:
        entries = AuditLogRepository(session).list_range()
    succeeded = [e for e in entries if e.subject_id == op_id and e.action == "operation.succeeded"]
    assert len(succeeded) == 1
    assert succeeded[0].detail.get("truncated") is True


# --------------------------------------------------------------------------------------
# Client cancellation cannot stop an in-flight execution: EXECUTING has no outbound
# transition to CANCELED in the state machine (BUILD_PLAN section 5.2).
# --------------------------------------------------------------------------------------


def test_cancel_cannot_interrupt_an_executing_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    with pytest.raises(InvalidStateTransitionError), session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")
    # The operation is still EXECUTING, unresolved — a disconnect only means the
    # client stopped listening, not that dispatch stopped happening.
    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "EXECUTING"


# --------------------------------------------------------------------------------------
# Concurrent execute calls on two *different* approved operations of the same workflow:
# max_concurrent enforcement, distinct from the same-operation handle-burn race already
# covered in test_core_service_operations.py.
# --------------------------------------------------------------------------------------


def test_concurrency_limit_blocks_a_second_operation_while_the_first_is_executing(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_a = _prepare_and_approve(session_factory, env, arguments={"email": "a@b.com"})
    op_b = _prepare_and_approve(session_factory, env, arguments={"email": "b@b.com"})
    _burn(session_factory, op_a)  # wf.dispatch's max_concurrent is 1; op_a now EXECUTING

    with pytest.raises(ConcurrencyLimitReachedError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_b,
            handle=op_b,
            principal_id="local",
            preflight=FakePreflight(),
        )
    with session_scope(session_factory) as session:
        operation_b = service.get_operation(session, operation_id=op_b, principal_id="local")
    assert operation_b.state == "APPROVED"  # refused before the burn; still executable later


def test_concurrent_execute_attempts_on_different_operations_burn_at_most_one(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """Under genuine thread concurrency (not just sequential calls): several distinct,
    already-approved operations of a ``max_concurrent: 1`` workflow race to execute;
    exactly one wins, every other loses to ``ConcurrencyLimitReachedError``."""
    op_ids = [
        _prepare_and_approve(session_factory, env, arguments={"email": f"{i}@b.com"})
        for i in range(6)
    ]

    def attempt(op_id: str) -> str:
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
        except ConcurrencyLimitReachedError:
            return "concurrency_limited"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, op_ids))

    assert results.count("success") == 1
    assert results.count("concurrency_limited") == 5


# --------------------------------------------------------------------------------------
# Rate limiting: enforced at prepare time, per workflow, across all principals.
# --------------------------------------------------------------------------------------


def test_rate_limit_is_enforced_at_prepare_time(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    # wf.rate_limited allows 1 per minute.
    _prepare(session_factory, env, workflow_id="wf.rate_limited", arguments={})
    with pytest.raises(RateLimitedError):
        _prepare(session_factory, env, workflow_id="wf.rate_limited", arguments={})


def test_rate_limit_window_is_per_workflow_not_per_operation(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """The rate limit is a property of the workflow, not the caller: a second principal
    hitting the same rate-limited workflow is refused too."""
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="other", kind="local", display_name="other")
    _prepare(session_factory, env, workflow_id="wf.rate_limited", arguments={})
    other_env = {"principal_id": "other", "environment": "default"}
    with pytest.raises(RateLimitedError):
        _prepare(session_factory, other_env, workflow_id="wf.rate_limited", arguments={})


# --------------------------------------------------------------------------------------
# Argument fingerprint re-verification: structurally unreachable in practice (nothing
# mutates stored arguments after creation) but verified explicitly, matching invariant
# I5 — proven here by mutating the stored row directly, the only way to exercise it.
# --------------------------------------------------------------------------------------


def test_execute_detects_a_tampered_argument_fingerprint(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    from sqlalchemy import update

    from n8n_operator.storage.models import Operation as OperationRow

    op_id = _prepare_and_approve(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(arguments={"email": "tampered@b.com"})
        )
    with pytest.raises(ArgumentMismatchError), session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=op_id,
            handle=op_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


# --------------------------------------------------------------------------------------
# No automatic retry, behaviorally: regardless of outcome kind, dispatch is called
# exactly once (ADR-005) — the runtime counterpart to the static grep-based contract
# test in tests/contract/test_n8n_no_retry.py.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["success", "error", "indeterminate"])
def test_dispatch_is_never_called_more_than_once_regardless_of_outcome(
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    kind: Literal["success", "error", "indeterminate"],
) -> None:
    op_id = _prepare_and_approve(session_factory, env)
    _burn(session_factory, op_id)
    dispatch = FakeDispatch(
        outcome=DispatchOutcome(
            kind=kind,
            http_status=None if kind == "indeterminate" else 200,
            result=None if kind == "indeterminate" else {"ok": True},
            execution_id=None,
            correlation_available=False,
        )
    )
    service.dispatch_operation(
        session_factory, operation_id=op_id, principal_id="local", dispatch=dispatch
    )
    assert dispatch.dispatch_calls == 1
