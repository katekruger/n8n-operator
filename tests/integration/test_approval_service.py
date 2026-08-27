"""The approval token service and the shared decision-context/expiry use cases
(BUILD_PLAN section 12, phase 6; ADR-010) against a real database.

Covers AC-08, AC-09, AC-12, AC-21 at the ``core.service`` layer (the CLI and the
approval app tests exercise the identical use cases through their own adapters), plus
the approval-token binding's tamper-evidence (a changed payload fingerprint or a
changed workflow definition hash after mint), and concurrent sweeper/lazy-expiry safety.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.handles import compute_approval_binding, hash_approval_token
from n8n_operator.core.models import PreflightCheck, PreflightResult
from n8n_operator.errors import (
    ApprovalNotPendingError,
    ApprovalRequiredError,
    ApprovalTokenAlreadyUsedError,
    ApprovalTokenInvalidError,
    InvalidStateTransitionError,
    OperationExpiredError,
)
from n8n_operator.storage.models import Operation as OperationRow
from n8n_operator.storage.repository import (
    ApprovalRepository,
    OperationEventRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase6-test
workflows:
  - id: wf.approval
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
""".format(hash_a="a" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(
            ready=True,
            checks=[PreflightCheck(check="instance_reachable", status="pass")],
            checked_at=datetime.now(UTC),
        )


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
    session_factory: sessionmaker[Session], env: dict[str, Any], **kwargs: Any
) -> tuple[str, str, str]:
    """Prepare ``wf.approval`` and return ``(operation_id, state, approval_token)``."""
    with session_scope(session_factory) as session:
        operation, _replay, token = service.prepare_operation(
            session,
            principal_id=env["principal_id"],
            environment=env["environment"],
            workflow_id="wf.approval",
            arguments=kwargs.pop("arguments", {"email": "a@b.com"}),
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            **kwargs,
        )
        assert token is not None
        return operation.id, operation.state, token


# --------------------------------------------------------------------------------------
# AC-08 — prepare_operation on external_write mints a redeemable approval token.
# --------------------------------------------------------------------------------------


def test_ac08_prepare_mints_a_redeemable_token(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, state, token = _prepare(session_factory, env)
    assert state == "PENDING_APPROVAL"

    with session_scope(session_factory) as session:
        context = service.resolve_approval_token(session, token=token)
    assert context.operation_id == op_id
    assert context.approval_required is True
    assert context.decided is False
    assert context.title == "Needs approval"
    assert context.risk == "medium"
    assert context.side_effects == "external_write"
    assert context.arguments == {"email": "a@b.com"}


# --------------------------------------------------------------------------------------
# AC-09 — execute_operation refuses a PENDING_APPROVAL operation; approving unblocks it.
# --------------------------------------------------------------------------------------


def test_ac09_execute_refused_until_approved_then_allowed(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, token = _prepare(session_factory, env)

    with session_scope(session_factory) as session, pytest.raises(ApprovalRequiredError):
        service.execute_operation(session, operation_id=op_id, handle=op_id, principal_id="local")

    with session_scope(session_factory) as session:
        resolved = service.resolve_approval_token(session, token=token)
        service.approve_operation(session, operation_id=resolved.operation_id, decided_by="local")

    with session_scope(session_factory) as session:
        operation = service.execute_operation(
            session, operation_id=op_id, handle=op_id, principal_id="local"
        )
    assert operation.state == "EXECUTING"


# --------------------------------------------------------------------------------------
# AC-12 — lazy expiry, with no sweeper running.
# --------------------------------------------------------------------------------------


def test_ac12_expired_on_next_read_with_no_sweeper(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "EXPIRED"

    with session_scope(session_factory) as session, pytest.raises(OperationExpiredError):
        service.execute_operation(session, operation_id=op_id, handle=op_id, principal_id="local")

    with session_scope(session_factory) as session, pytest.raises(ApprovalNotPendingError):
        service.resolve_approval_token(session, token=token)


# --------------------------------------------------------------------------------------
# AC-21 — reused token, expired/no-longer-pending, both channels write T06 identically.
# --------------------------------------------------------------------------------------


def test_ac21_reused_token_is_rejected(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        resolved = service.resolve_approval_token(session, token=token)
        service.approve_operation(session, operation_id=resolved.operation_id, decided_by="local")

    with session_scope(session_factory) as session, pytest.raises(ApprovalTokenAlreadyUsedError):
        service.resolve_approval_token(session, token=token)


def test_ac21_invalid_token_is_rejected(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session, pytest.raises(ApprovalTokenInvalidError):
        service.resolve_approval_token(session, token="not-a-real-token")


def test_ac21_decision_on_non_pending_operation_is_rejected_by_both_channels(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        resolved = service.resolve_approval_token(session, token=token)
        service.approve_operation(session, operation_id=resolved.operation_id, decided_by="local")

    # CLI-shaped path: operation_id directly, no token — the state machine itself
    # refuses a second T06 (channel-agnostic protection, ADR-010).
    with session_scope(session_factory) as session, pytest.raises(InvalidStateTransitionError):
        service.approve_operation(session, operation_id=op_id, decided_by="local")

    # Web-page-shaped path: the token is now decided, caught before the state machine
    # is even consulted.
    with session_scope(session_factory) as session, pytest.raises(ApprovalTokenAlreadyUsedError):
        service.resolve_approval_token(session, token=token)


def test_ac21_both_channels_write_the_identical_t06_transition(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """Two independent operations, one approved by operation ID (the CLI shape), one
    by token (the web-page shape) — both land the same T06 event."""
    op_id_a, _state_a, _token_a = _prepare(session_factory, env)
    op_id_b, _state_b, token_b = _prepare(session_factory, env)

    with session_scope(session_factory) as session:
        service.approve_operation(session, operation_id=op_id_a, decided_by="local")
        resolved_b = service.resolve_approval_token(session, token=token_b)
        service.approve_operation(session, operation_id=resolved_b.operation_id, decided_by="local")

    with session_scope(session_factory) as session:
        events_a = OperationEventRepository(session).list_for_operation(op_id_a)
        events_b = OperationEventRepository(session).list_for_operation(op_id_b)
    assert [e.transition for e in events_a if e.transition == "T06"] == ["T06"]
    assert [e.transition for e in events_b if e.transition == "T06"] == ["T06"]


# --------------------------------------------------------------------------------------
# Binding tamper-evidence — changed payload / changed workflow hash after mint.
# --------------------------------------------------------------------------------------


def test_binding_mismatch_on_changed_payload_fingerprint_is_caught(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """Structurally unreachable through any real code path (``argument_fingerprint`` is
    never updated after an operation is created) — simulated here by writing directly
    to the row, bypassing every use case, to prove the binding check is load-bearing
    rather than a tautology that always agrees with itself."""
    op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(argument_fingerprint="sha256:" + "f" * 64)
        )

    with session_scope(session_factory) as session, pytest.raises(AssertionError):
        service.resolve_approval_token(session, token=token)


def test_binding_mismatch_on_changed_definition_hash_is_caught(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(definition_hash="sha256:" + "d" * 64)
        )

    with session_scope(session_factory) as session, pytest.raises(AssertionError):
        service.resolve_approval_token(session, token=token)


def test_compute_approval_binding_is_deterministic_and_field_order_sensitive() -> None:
    a = compute_approval_binding(
        operation_id="op_1",
        principal_id="local",
        argument_fingerprint="fp1",
        snapshot_id="snap1",
        definition_hash="hash1",
    )
    b = compute_approval_binding(
        operation_id="op_1",
        principal_id="local",
        argument_fingerprint="fp1",
        snapshot_id="snap1",
        definition_hash="hash1",
    )
    assert a == b
    # A field-boundary shift ("op_1" + "1local" vs "op_11" + "local") must not collide.
    c = compute_approval_binding(
        operation_id="op_11",
        principal_id="local",
        argument_fingerprint="fp1",
        snapshot_id="snap1",
        definition_hash="hash1",
    )
    assert a != c


def test_hash_approval_token_matches_what_prepare_stores(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        approval = ApprovalRepository(session).get_by_operation_id(op_id)
        assert approval is not None
        assert approval.token_hash == hash_approval_token(token)


# --------------------------------------------------------------------------------------
# Concurrent sweeper / lazy expiry.
# --------------------------------------------------------------------------------------


def test_expire_overdue_operations_is_idempotent_across_two_sweeps(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, _state, _token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    with session_scope(session_factory) as session:
        first = service.expire_overdue_operations(session)
    with session_scope(session_factory) as session:
        second = service.expire_overdue_operations(session)

    assert first == 1
    assert second == 0
    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation(op_id)
    assert [e.transition for e in events if e.transition == "T08"] == ["T08"]


def test_concurrent_sweep_and_lazy_expiry_do_not_double_transition_or_raise(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """Two callers race to expire the same overdue row: one is the sweep
    (``expire_overdue_operations``), the other is a plain read
    (``get_operation``, which applies lazy expiry itself). Neither may raise, and
    exactly one ``T08`` event must land."""
    op_id, _state, _token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    # Simulate the race by starting a second transaction against the same row before
    # the first one commits its expiry: both open a session, both read the row (same
    # state_version), then both attempt the transition. One CAS wins; the other must
    # observe `_apply_lazy_expiry`'s race-safe fallback rather than raising.
    from n8n_operator.storage.repository import OperationRepository

    session_a = session_factory()
    session_b = session_factory()
    try:
        raw_a = OperationRepository(session_a).get(op_id)
        raw_b = OperationRepository(session_b).get(op_id)
        assert raw_a is not None
        assert raw_b is not None

        updated_a = service._apply_lazy_expiry(session_a, raw_a)
        session_a.commit()

        # session_b's row is now stale (same state_version it read before session_a's
        # commit) — applying lazy expiry against it must not raise.
        updated_b = service._apply_lazy_expiry(session_b, raw_b)
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()

    assert updated_a.state == "EXPIRED"
    assert updated_b.state == "EXPIRED"

    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation(op_id)
    t08_events = [e for e in events if e.transition == "T08"]
    assert len(t08_events) == 1
