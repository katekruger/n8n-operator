"""Data access. Portable SQL only.

Typed repository classes, one per table, each wrapping a single ``Session`` a caller
supplies (via :func:`~n8n_operator.storage.session.session_scope`). No repository method
opens or closes its own transaction — composing several of them inside one
``session_scope`` block is what makes a multi-row write atomic (invariant I6).

**No state-machine logic lives here.** ``OperationRepository`` knows how to move an
``operations`` row from one state to another under a compare-and-set guard and append the
matching event row in the same call — that is a storage-layer mechanism, not a policy
decision. It has no notion of which of the fifteen transitions in BUILD_PLAN section 5.2
are legal from which state, does not consult the transition table, and will happily
"apply" a transition a future caller should never have requested. Deciding *which*
transition is legal is exclusively ``core/state_machine.py``'s job (phase 3); this module
only guarantees that whichever one the caller decided on lands atomically or not at all.

No raw SQL string appears outside a migration (ADR-004 rule D5) — every statement here is
SQLAlchemy Core or ORM. Uniqueness is enforced by the database constraints declared in
``storage/models.py``, not by a read-then-check race in this module (rule D8).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Select, select, update
from sqlalchemy.orm import Session

from n8n_operator.errors import OptimisticLockError
from n8n_operator.storage.models import (
    GENESIS_HASH,
    Approval,
    AuditLogEntry,
    ExecutionResult,
    Operation,
    OperationEvent,
    Principal,
    RegistrySnapshot,
    WorkflowBinding,
    new_ulid,
    utc_now,
)


class PrincipalRepository:
    """The ``principals`` table. v1 holds exactly one row, ``kind='local'``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        kind: str,
        display_name: str,
        external_subject: str | None = None,
        id: str | None = None,  # noqa: A002 - matches the column name deliberately
    ) -> Principal:
        principal = Principal(
            id=id or new_ulid(),
            kind=kind,
            display_name=display_name,
            external_subject=external_subject,
        )
        self._session.add(principal)
        self._session.flush()
        return principal

    def get(self, principal_id: str) -> Principal | None:
        return self._session.get(Principal, principal_id)


class RegistrySnapshotRepository:
    """The ``registry_snapshots`` table (BUILD_PLAN section 6.7)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        content_hash: str,
        source_path: str,
        document: dict[str, Any],
        id: str | None = None,  # noqa: A002
    ) -> RegistrySnapshot:
        snapshot = RegistrySnapshot(
            id=id or new_ulid(),
            content_hash=content_hash,
            source_path=source_path,
            document=document,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get(self, snapshot_id: str) -> RegistrySnapshot | None:
        return self._session.get(RegistrySnapshot, snapshot_id)

    def get_by_content_hash(self, content_hash: str) -> RegistrySnapshot | None:
        stmt: Select[tuple[RegistrySnapshot]] = select(RegistrySnapshot).where(
            RegistrySnapshot.content_hash == content_hash
        )
        return self._session.scalars(stmt).one_or_none()

    def get_latest(self) -> RegistrySnapshot | None:
        """The "active" snapshot: whichever was loaded most recently.

        There is no separate mutable "active" pointer to manage (BUILD_PLAN section
        8.1 has no such column) — snapshots are immutable and append-only, so "active"
        is simply "the one with the greatest ``loaded_at``". ``None`` only before the
        first successful ``registry reload``.
        """
        stmt: Select[tuple[RegistrySnapshot]] = (
            select(RegistrySnapshot).order_by(RegistrySnapshot.loaded_at.desc()).limit(1)
        )
        return self._session.scalars(stmt).one_or_none()


class WorkflowBindingRepository:
    """The ``workflow_bindings`` table: one resolved entry within one snapshot."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        snapshot_id: str,
        workflow_id: str,
        n8n_workflow_id: str,
        definition_hash: str,
        side_effects: str,
        approval_policy: str,
        input_schema: dict[str, Any],
        id: str | None = None,  # noqa: A002
    ) -> WorkflowBinding:
        binding = WorkflowBinding(
            id=id or new_ulid(),
            snapshot_id=snapshot_id,
            workflow_id=workflow_id,
            n8n_workflow_id=n8n_workflow_id,
            definition_hash=definition_hash,
            side_effects=side_effects,
            approval_policy=approval_policy,
            input_schema=input_schema,
        )
        self._session.add(binding)
        self._session.flush()
        return binding

    def get_by_snapshot_and_workflow_id(
        self, snapshot_id: str, workflow_id: str
    ) -> WorkflowBinding | None:
        stmt: Select[tuple[WorkflowBinding]] = select(WorkflowBinding).where(
            WorkflowBinding.snapshot_id == snapshot_id,
            WorkflowBinding.workflow_id == workflow_id,
        )
        return self._session.scalars(stmt).one_or_none()


class OperationRepository:
    """The ``operations`` table and its append-only ``operation_events`` companion.

    Every write here is either a plain insert or a compare-and-set update. Nothing
    queries "is this transition legal" — see the module docstring.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        id: str,  # noqa: A002 - the op_<ULID> handle; minted by the caller (see models.py)
        principal_id: str,
        environment: str,
        snapshot_id: str,
        workflow_id: str,
        definition_hash: str,
        state: str,
        arguments: dict[str, Any],
        argument_fingerprint: str,
        argument_bytes: int,
        idempotency_key: str | None = None,
        approval_expires_at: datetime | None = None,
        execution_deadline: datetime | None = None,
    ) -> Operation:
        """Insert a new operation row at ``state_version=1``.

        Does not append an event row for this creation — callers that want the T01 event
        recorded atomically alongside creation should do so explicitly in the same
        session (this mirrors :meth:`apply_transition`, which never creates the row it
        transitions, only ever updates one that already exists).
        """
        operation = Operation(
            id=id,
            principal_id=principal_id,
            environment=environment,
            snapshot_id=snapshot_id,
            workflow_id=workflow_id,
            definition_hash=definition_hash,
            state=state,
            state_version=1,
            arguments=arguments,
            argument_fingerprint=argument_fingerprint,
            argument_bytes=argument_bytes,
            idempotency_key=idempotency_key,
            approval_expires_at=approval_expires_at,
            execution_deadline=execution_deadline,
        )
        self._session.add(operation)
        self._session.flush()
        return operation

    def get(self, operation_id: str) -> Operation | None:
        return self._session.get(Operation, operation_id)

    def find_by_idempotency(
        self, *, principal_id: str, environment: str, workflow_id: str, idempotency_key: str
    ) -> Operation | None:
        """Look up an operation by its full idempotency namespace (ADR-011).

        Only ever called with a non-``None`` key: two rows sharing a namespace with no
        key set are never duplicates of each other (see the module docstring on
        ``storage/models.py``), so there is nothing meaningful to "find" by a null key.
        """
        stmt: Select[tuple[Operation]] = select(Operation).where(
            Operation.principal_id == principal_id,
            Operation.environment == environment,
            Operation.workflow_id == workflow_id,
            Operation.idempotency_key == idempotency_key,
        )
        return self._session.scalars(stmt).one_or_none()

    def list(
        self,
        *,
        principal_id: str,
        environment: str | None = None,
        workflow_id: str | None = None,
        states: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[Operation]:
        """Filtered, most-recent-first history for one principal (MCP_TOOLS.md 2.10).

        Applies no policy of its own (per the module docstring) — including no lazy
        expiry, which is a state-machine concern; a caller that needs every returned row
        current must apply it itself, per row, after this query returns.
        """
        stmt: Select[tuple[Operation]] = select(Operation).where(
            Operation.principal_id == principal_id
        )
        if environment is not None:
            stmt = stmt.where(Operation.environment == environment)
        if workflow_id is not None:
            stmt = stmt.where(Operation.workflow_id == workflow_id)
        if states:
            stmt = stmt.where(Operation.state.in_(states))
        if since is not None:
            stmt = stmt.where(Operation.created_at >= since)
        stmt = stmt.order_by(Operation.created_at.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def compare_and_set_state(
        self,
        *,
        operation_id: str,
        expected_version: int,
        new_state: str,
        **field_updates: Any,
    ) -> Operation:
        """Move ``operation_id`` to ``new_state`` iff it is still at ``expected_version``.

        A plain, portable ``UPDATE ... WHERE id = :id AND state_version = :expected``
        (no ``RETURNING`` — ADR-004 rule D4), whose affected-row count is checked. Zero
        rows means the precondition did not hold — the row moved, or never existed — and
        raises :class:`~n8n_operator.errors.OptimisticLockError` rather than silently
        doing nothing. On success, ``state_version`` is incremented and ``updated_at`` is
        refreshed; any extra ``field_updates`` (e.g. ``approval_expires_at=None``) are
        applied in the same statement, still gated by the same precondition.

        This method does not know whether ``new_state`` is a legal successor of the row's
        current state — that check belongs to ``core/state_machine.py`` (phase 3), which
        is expected to call this only after deciding a transition is legal, never before.
        """
        values: dict[str, Any] = {
            "state": new_state,
            "state_version": expected_version + 1,
            "updated_at": utc_now(),
            **field_updates,
        }
        stmt = (
            update(Operation)
            .where(Operation.id == operation_id, Operation.state_version == expected_version)
            .values(**values)
        )
        # `Session.execute` on an `Update` is a `CursorResult` at runtime; SQLAlchemy's
        # overloads don't always narrow to that once `.values(**values)` has erased the
        # more specific generic, so the cast just states what is already true.
        result: CursorResult[Any] = cast("CursorResult[Any]", self._session.execute(stmt))
        if result.rowcount != 1:
            raise OptimisticLockError(
                "compare-and-set failed: operation is not at the expected version",
                details={"operation_id": operation_id, "expected_version": expected_version},
            )
        self._session.flush()
        updated = self.get(operation_id)
        assert updated is not None  # the CAS above just confirmed the row exists
        return updated

    def burn_handle(self, *, operation_id: str, burned_at: datetime | None = None) -> bool:
        """Burn the single-use operation handle (ADR-003, invariant I4).

        A dedicated compare-and-set distinct from :meth:`compare_and_set_state`, because
        burning is unconditional on the *handle* rather than on ``state_version`` — the
        guard is ``handle_burned_at IS NULL``. Returns ``True`` if this call burned it
        (exactly one row affected) and ``False`` if it was already burned (zero rows
        affected) — the caller distinguishes "I burned it" from "someone already did"
        without a second query.
        """
        stmt = (
            update(Operation)
            .where(Operation.id == operation_id, Operation.handle_burned_at.is_(None))
            .values(handle_burned_at=burned_at or utc_now())
        )
        result: CursorResult[Any] = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return result.rowcount == 1

    def apply_transition(
        self,
        *,
        operation_id: str,
        expected_version: int,
        new_state: str,
        transition: str,
        from_state: str | None,
        actor: str,
        detail: dict[str, Any] | None = None,
        **field_updates: Any,
    ) -> tuple[Operation, OperationEvent]:
        """Compare-and-set the operation's state and append its event row, together.

        The two writes are not individually transactional here — atomicity comes from
        the caller running this inside one ``session_scope`` block alongside whatever
        ``audit_log`` insert accompanies it (invariant I6). This method only guarantees
        that if the compare-and-set fails, no event row is appended for a transition that
        did not actually happen.
        """
        operation = self.compare_and_set_state(
            operation_id=operation_id,
            expected_version=expected_version,
            new_state=new_state,
            **field_updates,
        )
        event = OperationEvent(
            id=new_ulid(),
            operation_id=operation_id,
            from_state=from_state,
            to_state=new_state,
            transition=transition,
            actor=actor,
            detail=detail or {},
        )
        self._session.add(event)
        self._session.flush()
        return operation, event


class OperationEventRepository:
    """The append-only ``operation_events`` table. No update or delete method exists."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        operation_id: str,
        from_state: str | None,
        to_state: str,
        transition: str,
        actor: str,
        detail: dict[str, Any] | None = None,
        id: str | None = None,  # noqa: A002
    ) -> OperationEvent:
        event = OperationEvent(
            id=id or new_ulid(),
            operation_id=operation_id,
            from_state=from_state,
            to_state=to_state,
            transition=transition,
            actor=actor,
            detail=detail or {},
        )
        self._session.add(event)
        self._session.flush()
        return event

    def list_for_operation(self, operation_id: str) -> list[OperationEvent]:
        stmt: Select[tuple[OperationEvent]] = (
            select(OperationEvent)
            .where(OperationEvent.operation_id == operation_id)
            .order_by(OperationEvent.id)
        )
        return list(self._session.scalars(stmt))


class ApprovalRepository:
    """The ``approvals`` table. The token itself is never stored, only its hash."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        operation_id: str,
        token_hash: str,
        expires_at: datetime,
        id: str | None = None,  # noqa: A002
    ) -> Approval:
        approval = Approval(
            id=id or new_ulid(),
            operation_id=operation_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(approval)
        self._session.flush()
        return approval

    def get_by_token_hash(self, token_hash: str) -> Approval | None:
        stmt: Select[tuple[Approval]] = select(Approval).where(Approval.token_hash == token_hash)
        return self._session.scalars(stmt).one_or_none()

    def get_by_operation_id(self, operation_id: str) -> Approval | None:
        """The approval row minted for ``operation_id`` at T04, if this operation ever
        entered ``PENDING_APPROVAL`` (an auto-approved T05 operation has none)."""
        stmt: Select[tuple[Approval]] = select(Approval).where(
            Approval.operation_id == operation_id
        )
        return self._session.scalars(stmt).one_or_none()

    def record_decision(
        self,
        *,
        approval_id: str,
        decision: str,
        decided_by: str,
        decided_at: datetime | None = None,
    ) -> Approval:
        approval = self._session.get(Approval, approval_id)
        if approval is None:
            raise LookupError(f"no such approval: {approval_id}")
        approval.decision = decision
        approval.decided_by = decided_by
        approval.decided_at = decided_at or utc_now()
        self._session.flush()
        return approval


class ExecutionResultRepository:
    """The ``execution_results`` table. One row per operation — v1 never retries."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        operation_id: str,
        status: str,
        n8n_execution_id: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        redacted_payload: dict[str, Any] | None = None,
        node_trace: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult(
            operation_id=operation_id,
            status=status,
            n8n_execution_id=n8n_execution_id,
            started_at=started_at,
            finished_at=finished_at,
            redacted_payload=redacted_payload or {},
            node_trace=node_trace,
            error=error,
        )
        self._session.add(result)
        self._session.flush()
        return result

    def get(self, operation_id: str) -> ExecutionResult | None:
        return self._session.get(ExecutionResult, operation_id)


class AuditLogRepository:
    """The append-only, hash-chained ``audit_log`` table.

    This is a pure storage primitive: it inserts whatever ``prev_hash``/``entry_hash`` it
    is given. Computing those hashes from the entry's canonical content is
    ``audit/chain.py``'s and ``audit/writer.py``'s job (phase 3) — this class has no
    opinion on the hashing algorithm, only on making the append itself a plain,
    unconditional insert with no update or delete method anywhere on it (boundary B11).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        prev_hash: str,
        entry_hash: str,
        actor: str,
        action: str,
        subject_type: str,
        subject_id: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditLogEntry:
        """Insert one row. ``entry_hash`` is trusted as given — this class has no
        opinion on the hashing algorithm (see the class docstring).

        ``occurred_at`` defaults to the model's own ``utc_now()`` default when omitted,
        but a caller that already hashed a specific timestamp (``audit/writer.py``,
        always) must pass that exact value: the model's default is computed fresh at
        instantiation, a moment later than whatever the caller hashed, and the two would
        silently disagree — breaking chain verification on every single entry — if the
        stored value were left to default independently of what was hashed.
        """
        entry = AuditLogEntry(
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            outcome=outcome,
            detail=detail or {},
        )
        if occurred_at is not None:
            entry.occurred_at = occurred_at
        self._session.add(entry)
        self._session.flush()
        return entry

    def get_last(self) -> AuditLogEntry | None:
        stmt: Select[tuple[AuditLogEntry]] = (
            select(AuditLogEntry).order_by(AuditLogEntry.seq.desc()).limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def get_last_hash(self) -> str:
        """The most recent ``entry_hash``, or the genesis hash if the chain is empty."""
        last = self.get_last()
        return last.entry_hash if last is not None else GENESIS_HASH

    def list_range(self, *, start_seq: int = 1, limit: int = 100) -> list[AuditLogEntry]:
        stmt: Select[tuple[AuditLogEntry]] = (
            select(AuditLogEntry)
            .where(AuditLogEntry.seq >= start_seq)
            .order_by(AuditLogEntry.seq)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


__all__ = [
    "ApprovalRepository",
    "AuditLogRepository",
    "ExecutionResultRepository",
    "OperationEventRepository",
    "OperationRepository",
    "PrincipalRepository",
    "RegistrySnapshotRepository",
    "WorkflowBindingRepository",
]
