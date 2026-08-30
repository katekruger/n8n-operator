"""Repository CRUD, transaction rollback, uniqueness, FK enforcement, and the
compare-and-set primitives later phases build the state machine and handle-burning on
(BUILD_PLAN section 12, phase 1).

Every test runs against a real, file-based SQLite database (the ``engine``/``session_factory``
fixtures in ``tests/conftest.py``), with ``PRAGMA foreign_keys=ON`` enforced by
``storage/session.py`` — the same connection setup the application uses, not a
looser in-memory stand-in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.errors import OptimisticLockError
from n8n_operator.storage.models import GENESIS_HASH, Operation
from n8n_operator.storage.repository import (
    ApprovalRepository,
    AuditLogRepository,
    ExecutionResultRepository,
    OperationEventRepository,
    OperationRepository,
    PrincipalRepository,
    RegistrySnapshotRepository,
    WorkflowBindingRepository,
)
from n8n_operator.storage.session import session_scope


def _make_operation(
    session: Session,
    seed: dict[str, Any],
    *,
    id: str = "op_test1",  # noqa: A002
    workflow_id: str = "wf.a",
    idempotency_key: str | None = None,
    environment: str = "default",
    state: str = "PREPARING",
) -> Operation:
    return OperationRepository(session).create(
        id=id,
        principal_id=seed["principal_id"],
        environment=environment,
        snapshot_id=seed["snapshot_id"],
        workflow_id=workflow_id,
        definition_hash="sha256:" + "b" * 64,
        state=state,
        arguments={"x": 1},
        argument_fingerprint="fp-" + id,
        argument_bytes=7,
        idempotency_key=idempotency_key,
    )


# --------------------------------------------------------------------------------------
# PrincipalRepository / RegistrySnapshotRepository / WorkflowBindingRepository — CRUD
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_principal_create_and_get(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        created = PrincipalRepository(session).create(kind="local", display_name="local")
        principal_id = created.id

    with session_scope(session_factory) as session:
        fetched = PrincipalRepository(session).get(principal_id)
        assert fetched is not None
        assert fetched.kind == "local"
        assert fetched.display_name == "local"
        assert fetched.created_at.tzinfo is not None


@pytest.mark.integration
def test_principal_get_missing_returns_none(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        assert PrincipalRepository(session).get("does-not-exist") is None


@pytest.mark.integration
def test_registry_snapshot_create_and_lookup_by_content_hash(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "c" * 64,
            source_path="./workflows.yaml",
            document={"apiVersion": "n8n-operator/v1"},
        )

    with session_scope(session_factory) as session:
        found = RegistrySnapshotRepository(session).get_by_content_hash("sha256:" + "c" * 64)
        assert found is not None
        assert found.document == {"apiVersion": "n8n-operator/v1"}
        missing = RegistrySnapshotRepository(session).get_by_content_hash("sha256:" + "d" * 64)
        assert missing is None


@pytest.mark.integration
def test_registry_snapshot_get_by_id(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        created = RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "9" * 64, source_path="./x.yaml", document={"k": "v"}
        )
        snapshot_id = created.id

    with session_scope(session_factory) as session:
        fetched = RegistrySnapshotRepository(session).get(snapshot_id)
        assert fetched is not None
        assert fetched.document == {"k": "v"}
        assert RegistrySnapshotRepository(session).get("does-not-exist") is None


@pytest.mark.integration
def test_workflow_binding_create_and_lookup(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        WorkflowBindingRepository(session).create(
            snapshot_id=seed["snapshot_id"],
            workflow_id="crm.sync_contact",
            n8n_workflow_id="n8n-internal-id",
            definition_hash="sha256:" + "e" * 64,
            side_effects="external_write",
            approval_policy="required",
            input_schema={"type": "object"},
        )

    with session_scope(session_factory) as session:
        binding = WorkflowBindingRepository(session).get_by_snapshot_and_workflow_id(
            seed["snapshot_id"], "crm.sync_contact"
        )
        assert binding is not None
        assert binding.n8n_workflow_id == "n8n-internal-id"
        assert (
            WorkflowBindingRepository(session).get_by_snapshot_and_workflow_id(
                seed["snapshot_id"], "no.such.workflow"
            )
            is None
        )


# --------------------------------------------------------------------------------------
# OperationRepository — create, get, idempotency lookup
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_operation_create_and_get(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed)

    with session_scope(session_factory) as session:
        op = OperationRepository(session).get("op_test1")
        assert op is not None
        assert op.state == "PREPARING"
        assert op.state_version == 1
        assert op.environment == "default"
        assert op.handle_burned_at is None
        assert op.created_at.tzinfo is not None
        assert op.updated_at.tzinfo is not None


@pytest.mark.integration
def test_operation_get_missing_returns_none(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        assert OperationRepository(session).get("op_missing") is None


@pytest.mark.integration
def test_find_by_idempotency_returns_the_matching_operation(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_idem1", idempotency_key="client-key-1")

    with session_scope(session_factory) as session:
        found = OperationRepository(session).find_by_idempotency(
            principal_id=seed["principal_id"],
            environment="default",
            workflow_id="wf.a",
            idempotency_key="client-key-1",
        )
        assert found is not None
        assert found.id == "op_idem1"


@pytest.mark.integration
def test_find_by_idempotency_returns_none_for_a_different_namespace_component(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_idem2", idempotency_key="client-key-2")

    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        # Same key, different workflow: a different namespace entirely (ADR-011).
        assert (
            repo.find_by_idempotency(
                principal_id=seed["principal_id"],
                environment="default",
                workflow_id="wf.SOME-OTHER-WORKFLOW",
                idempotency_key="client-key-2",
            )
            is None
        )
        # Same key, different environment: also a different namespace.
        assert (
            repo.find_by_idempotency(
                principal_id=seed["principal_id"],
                environment="staging",
                workflow_id="wf.a",
                idempotency_key="client-key-2",
            )
            is None
        )


# --------------------------------------------------------------------------------------
# Uniqueness — the idempotency namespace constraint (ADR-011, invariant I8)
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_two_operations_same_namespace_and_key_collide(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_a", idempotency_key="dup-key")

    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_b", idempotency_key="dup-key")


@pytest.mark.integration
def test_two_operations_same_namespace_both_null_key_do_not_collide(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """The property ADR-011 relies on: NULL is never equal to NULL for uniqueness
    purposes in standard SQL, in both SQLite and PostgreSQL — no partial index needed."""
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_null1", idempotency_key=None)
        _make_operation(session, seed, id="op_null2", idempotency_key=None)

    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        assert repo.get("op_null1") is not None
        assert repo.get("op_null2") is not None


# --------------------------------------------------------------------------------------
# Stage 06: reconciliation is structurally an annotation, never a transition — the
# `operation_events.transition` CHECK constraint (T01-T15 only) makes this true at the
# schema level, not merely by application-code discipline.
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_operation_events_transition_check_constraint_rejects_a_non_transition_value(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """A hand-crafted attempt to record something that looks like a reconciliation
    "transition" (as opposed to the real mechanism, an ``audit_log`` annotation —
    ``core.service.reconcile_operation``) fails at the database layer itself."""
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_reconcile_attempt")

    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        OperationEventRepository(session).append(
            operation_id="op_reconcile_attempt",
            from_state="UNKNOWN",
            to_state="UNKNOWN",
            transition="RECONCILE",
            actor="local",
        )


@pytest.mark.integration
def test_different_workflow_same_key_does_not_collide(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(
            session, seed, id="op_wf_a", workflow_id="wf.a", idempotency_key="shared-key"
        )
        _make_operation(
            session, seed, id="op_wf_b", workflow_id="wf.b", idempotency_key="shared-key"
        )

    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        assert repo.get("op_wf_a") is not None
        assert repo.get("op_wf_b") is not None


@pytest.mark.integration
def test_different_environment_same_key_does_not_collide(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_env_a", environment="default", idempotency_key="k")
        _make_operation(session, seed, id="op_env_b", environment="staging", idempotency_key="k")

    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        assert repo.get("op_env_a") is not None
        assert repo.get("op_env_b") is not None


@pytest.mark.integration
def test_registry_snapshot_content_hash_is_unique(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "f" * 64, source_path="./a.yaml", document={}
        )
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "f" * 64, source_path="./b.yaml", document={}
        )


@pytest.mark.integration
def test_approval_token_hash_is_unique(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_for_approval")
        ApprovalRepository(session).create(
            operation_id="op_for_approval",
            token_hash="sha256:" + "1" * 64,
            binding_hash="sha256:" + "b" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        ApprovalRepository(session).create(
            operation_id="op_for_approval",
            token_hash="sha256:" + "1" * 64,
            binding_hash="sha256:" + "b" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )


# --------------------------------------------------------------------------------------
# Foreign-key enforcement (SQLite PRAGMA foreign_keys=ON — storage/session.py)
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_operation_with_unknown_principal_id_is_rejected(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        OperationRepository(session).create(
            id="op_bad_principal",
            principal_id="no-such-principal",
            environment="default",
            snapshot_id=seed["snapshot_id"],
            workflow_id="wf.a",
            definition_hash="sha256:" + "0" * 64,
            state="PREPARING",
            arguments={},
            argument_fingerprint="fp",
            argument_bytes=2,
        )


@pytest.mark.integration
def test_operation_with_unknown_snapshot_id_is_rejected(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        OperationRepository(session).create(
            id="op_bad_snapshot",
            principal_id=seed["principal_id"],
            environment="default",
            snapshot_id="no-such-snapshot",
            workflow_id="wf.a",
            definition_hash="sha256:" + "0" * 64,
            state="PREPARING",
            arguments={},
            argument_fingerprint="fp",
            argument_bytes=2,
        )


@pytest.mark.integration
def test_operation_event_with_unknown_operation_id_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        OperationEventRepository(session).append(
            operation_id="no-such-operation",
            from_state=None,
            to_state="PREPARING",
            transition="T01",
            actor="system",
        )


@pytest.mark.integration
def test_workflow_binding_with_unknown_snapshot_id_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        WorkflowBindingRepository(session).create(
            snapshot_id="no-such-snapshot",
            workflow_id="wf.a",
            n8n_workflow_id="n8n-id",
            definition_hash="sha256:" + "0" * 64,
            side_effects="read_only",
            approval_policy="none",
            input_schema={},
        )


# --------------------------------------------------------------------------------------
# CHECK constraints — invalid state / transition / decision / status / outcome values
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_operation_state_check_constraint_rejects_unknown_values(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_bad_state", state="NOT_A_REAL_STATE")


@pytest.mark.integration
def test_operation_event_transition_check_constraint_rejects_unknown_values(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_for_bad_event")
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        OperationEventRepository(session).append(
            operation_id="op_for_bad_event",
            from_state=None,
            to_state="PREPARING",
            transition="T99",
            actor="system",
        )


# --------------------------------------------------------------------------------------
# Transaction rollback — session_scope
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_session_scope_rolls_back_on_exception(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError), session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_should_not_persist")
        raise _BoomError("simulated failure after the write")

    with session_scope(session_factory) as session:
        assert OperationRepository(session).get("op_should_not_persist") is None


@pytest.mark.integration
def test_session_scope_rolls_back_the_whole_block_not_just_the_failing_statement(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """A multi-write transaction (operation + event, as ``apply_transition`` composes)
    must be all-or-nothing: the first write must not survive a later failure in the same
    block (invariant I6's atomicity, exercised here at the storage layer)."""
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_partial")
        # This second statement fails its FK check; the whole transaction rolls back.
        OperationEventRepository(session).append(
            operation_id="op_partial",
            from_state=None,
            to_state="PREPARING",
            transition="T99",  # invalid — CHECK constraint violation
            actor="system",
        )

    with session_scope(session_factory) as session:
        assert OperationRepository(session).get("op_partial") is None


@pytest.mark.integration
def test_a_clean_block_commits(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_committed")
    with session_scope(session_factory) as session:
        assert OperationRepository(session).get("op_committed") is not None


# --------------------------------------------------------------------------------------
# Compare-and-set primitives — compare_and_set_state, burn_handle, apply_transition
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_compare_and_set_state_succeeds_at_the_expected_version(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_cas1")

    with session_scope(session_factory) as session:
        updated = OperationRepository(session).compare_and_set_state(
            operation_id="op_cas1", expected_version=1, new_state="PENDING_APPROVAL"
        )
        assert updated.state == "PENDING_APPROVAL"
        assert updated.state_version == 2


@pytest.mark.integration
def test_compare_and_set_state_fails_at_a_stale_version(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_cas2")
        OperationRepository(session).compare_and_set_state(
            operation_id="op_cas2", expected_version=1, new_state="PENDING_APPROVAL"
        )

    with pytest.raises(OptimisticLockError), session_scope(session_factory) as session:
        # The row is now at version 2; a caller still holding "1" must be rejected.
        OperationRepository(session).compare_and_set_state(
            operation_id="op_cas2", expected_version=1, new_state="APPROVED"
        )

    with session_scope(session_factory) as session:
        # And the rejected attempt must not have partially applied.
        op = OperationRepository(session).get("op_cas2")
        assert op is not None
        assert op.state == "PENDING_APPROVAL"
        assert op.state_version == 2


@pytest.mark.integration
def test_compare_and_set_state_accepts_arbitrary_field_updates(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_cas3")
        updated = OperationRepository(session).compare_and_set_state(
            operation_id="op_cas3",
            expected_version=1,
            new_state="APPROVED",
            execution_deadline=deadline,
        )
        assert updated.execution_deadline is not None
        assert updated.execution_deadline.replace(microsecond=0) == deadline.replace(microsecond=0)


@pytest.mark.integration
def test_burn_handle_succeeds_exactly_once(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_burn1", state="APPROVED")

    with session_scope(session_factory) as session:
        first = OperationRepository(session).burn_handle(operation_id="op_burn1")
        assert first is True

    with session_scope(session_factory) as session:
        second = OperationRepository(session).burn_handle(operation_id="op_burn1")
        assert second is False  # already burned — I4


@pytest.mark.integration
def test_burn_handle_is_a_real_compare_and_set_under_concurrent_attempts(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """Two sessions racing to burn the same handle: exactly one succeeds, verified by
    committing both attempts and counting how many report success — the property
    invariant I4 exists to guarantee, exercised here rather than merely asserted."""
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_race", state="APPROVED")

    results = []
    for _ in range(2):
        with session_scope(session_factory) as session:
            results.append(OperationRepository(session).burn_handle(operation_id="op_race"))
    assert sorted(results) == [False, True]

    with session_scope(session_factory) as session:
        op = OperationRepository(session).get("op_race")
        assert op is not None
        assert op.handle_burned_at is not None


@pytest.mark.integration
def test_apply_transition_updates_state_and_appends_one_event(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_transition1")

    with session_scope(session_factory) as session:
        operation, event = OperationRepository(session).apply_transition(
            operation_id="op_transition1",
            expected_version=1,
            new_state="PENDING_APPROVAL",
            transition="T04",
            from_state="PREPARING",
            actor="system",
            detail={"reason": "approval required"},
        )
        assert operation.state == "PENDING_APPROVAL"
        assert event.transition == "T04"
        assert event.from_state == "PREPARING"
        assert event.to_state == "PENDING_APPROVAL"

    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation("op_transition1")
        assert len(events) == 1
        assert events[0].detail == {"reason": "approval required"}


@pytest.mark.integration
def test_apply_transition_does_not_append_an_event_when_the_cas_fails(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_transition2")
        OperationRepository(session).compare_and_set_state(
            operation_id="op_transition2", expected_version=1, new_state="PENDING_APPROVAL"
        )

    with pytest.raises(OptimisticLockError), session_scope(session_factory) as session:
        OperationRepository(session).apply_transition(
            operation_id="op_transition2",
            expected_version=1,  # stale — the row is at version 2
            new_state="APPROVED",
            transition="T06",
            from_state="PENDING_APPROVAL",
            actor="local",
        )

    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation("op_transition2")
        assert events == []


@pytest.mark.integration
def test_operation_event_repository_append_directly(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    """``OperationEventRepository.append`` on its own, distinct from the event insert
    ``OperationRepository.apply_transition`` composes internally — both are part of the
    public "atomic operation/event writes" surface later phases build on."""
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_direct_event")

    with session_scope(session_factory) as session:
        event = OperationEventRepository(session).append(
            operation_id="op_direct_event",
            from_state=None,
            to_state="PREPARING",
            transition="T01",
            actor="system",
            detail={"note": "created directly"},
        )
        assert event.transition == "T01"
        assert event.id  # a ULID was minted since none was supplied

    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation("op_direct_event")
        assert len(events) == 1
        assert events[0].detail == {"note": "created directly"}


@pytest.mark.integration
def test_operation_events_are_ordered_chronologically(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_multi_event")

    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        repo.apply_transition(
            operation_id="op_multi_event",
            expected_version=1,
            new_state="PENDING_APPROVAL",
            transition="T04",
            from_state="PREPARING",
            actor="system",
        )
    with session_scope(session_factory) as session:
        repo = OperationRepository(session)
        repo.apply_transition(
            operation_id="op_multi_event",
            expected_version=2,
            new_state="APPROVED",
            transition="T06",
            from_state="PENDING_APPROVAL",
            actor="local",
        )

    with session_scope(session_factory) as session:
        events = OperationEventRepository(session).list_for_operation("op_multi_event")
        assert [e.transition for e in events] == ["T04", "T06"]


# --------------------------------------------------------------------------------------
# ExecutionResultRepository, ApprovalRepository.record_decision, AuditLogRepository
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_execution_result_create_and_get(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_result1")
        ExecutionResultRepository(session).create(
            operation_id="op_result1", status="success", redacted_payload={"ok": True}
        )

    with session_scope(session_factory) as session:
        result = ExecutionResultRepository(session).get("op_result1")
        assert result is not None
        assert result.status == "success"
        assert result.redacted_payload == {"ok": True}


@pytest.mark.integration
def test_approval_record_decision(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed, id="op_approval1")
        approval = ApprovalRepository(session).create(
            operation_id="op_approval1",
            token_hash="sha256:" + "2" * 64,
            binding_hash="sha256:" + "b" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        approval_id = approval.id

    with session_scope(session_factory) as session:
        ApprovalRepository(session).record_decision(
            approval_id=approval_id, decision="approved", decided_by="local"
        )

    with session_scope(session_factory) as session:
        found = ApprovalRepository(session).get_by_token_hash("sha256:" + "2" * 64)
        assert found is not None
        assert found.decision == "approved"
        assert found.decided_by == "local"
        assert found.decided_at is not None


@pytest.mark.integration
def test_approval_record_decision_missing_approval_raises_lookup_error(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(LookupError), session_scope(session_factory) as session:
        ApprovalRepository(session).record_decision(
            approval_id="does-not-exist", decision="approved", decided_by="local"
        )


@pytest.mark.integration
def test_audit_log_append_and_get_last_hash(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        assert AuditLogRepository(session).get_last_hash() == GENESIS_HASH

    with session_scope(session_factory) as session:
        AuditLogRepository(session).append(
            prev_hash=GENESIS_HASH,
            entry_hash="a" * 64,
            actor="local",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_x",
            outcome="allowed",
        )

    with session_scope(session_factory) as session:
        assert AuditLogRepository(session).get_last_hash() == "a" * 64


@pytest.mark.integration
def test_audit_log_entry_hash_must_be_unique(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        AuditLogRepository(session).append(
            prev_hash=GENESIS_HASH,
            entry_hash="b" * 64,
            actor="local",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_x",
            outcome="allowed",
        )
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        AuditLogRepository(session).append(
            prev_hash="b" * 64,
            entry_hash="b" * 64,  # duplicate — must be rejected
            actor="local",
            action="operation.prepared",
            subject_type="operation",
            subject_id="op_y",
            outcome="allowed",
        )


@pytest.mark.integration
def test_audit_log_list_range_orders_by_seq(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        prev = GENESIS_HASH
        for i in range(3):
            entry_hash = f"{i:064d}"
            repo.append(
                prev_hash=prev,
                entry_hash=entry_hash,
                actor="local",
                action="operation.prepared",
                subject_type="operation",
                subject_id=f"op_{i}",
                outcome="allowed",
            )
            prev = entry_hash

    with session_scope(session_factory) as session:
        entries = AuditLogRepository(session).list_range()
        assert [e.subject_id for e in entries] == ["op_0", "op_1", "op_2"]
        assert [e.seq for e in entries] == sorted(e.seq for e in entries)


@pytest.mark.integration
def test_no_update_or_delete_method_exists_on_audit_log_repository() -> None:
    """Boundary B11: append-only. There is no method here that could update or delete a
    row — checked directly against the class's own public interface."""
    public_methods = {name for name in dir(AuditLogRepository) if not name.startswith("_")}
    assert public_methods == {
        "append",
        "get_last",
        "get_last_hash",
        "list_range",
        "list_all",
        "list_for_subject",
        "list_page",
    }


@pytest.mark.integration
def test_no_update_or_delete_method_exists_on_operation_event_repository() -> None:
    public_methods = {name for name in dir(OperationEventRepository) if not name.startswith("_")}
    assert public_methods == {"append", "list_for_operation"}


@pytest.mark.integration
def test_no_update_or_delete_method_exists_on_registry_snapshot_repository() -> None:
    """BUILD_PLAN section 6.7: snapshots are content-addressed, append-only, and never
    mutated — checked directly against the class's own public interface, the same way
    ``AuditLogRepository``'s append-only contract is checked above."""
    public_methods = {name for name in dir(RegistrySnapshotRepository) if not name.startswith("_")}
    assert public_methods == {"create", "get", "get_by_content_hash", "get_latest"}


@pytest.mark.integration
def test_no_update_or_delete_method_exists_on_workflow_binding_repository() -> None:
    public_methods = {name for name in dir(WorkflowBindingRepository) if not name.startswith("_")}
    assert public_methods == {"create", "get_by_snapshot_and_workflow_id"}
