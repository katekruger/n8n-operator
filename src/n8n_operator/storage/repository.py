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

import builtins
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Select, false, func, or_, select, true, update
from sqlalchemy.orm import Session

from n8n_operator.errors import OptimisticLockError
from n8n_operator.storage.models import (
    GENESIS_HASH,
    Approval,
    AuditLogEntry,
    Environment,
    ExecutionResult,
    NotificationDelivery,
    Operation,
    OperationEvent,
    Organization,
    OrganizationMembership,
    Principal,
    RegistrySnapshot,
    WorkflowBinding,
    WorkflowDefinitionSnapshot,
    WorkflowEnvironmentOverlay,
    new_ulid,
    utc_now,
)


class PrincipalRepository:
    """The ``principals`` table. v1 holds exactly one row, ``kind='local'``.

    Stage 02 (ADR-013, ADR-014) adds ``kind='user'``/``kind='service'`` rows:
    ``get_by_external_identity`` is the JIT-provisioning lookup keyed on ``(iss, sub)``
    (never ``sub`` alone), ``disable``/``enable`` set/clear ``disabled_at`` (checked
    live on every request, never cached — ADR-014 section 4), and
    ``set_credential_ref`` repoints a service principal's ``env:``/``keyring:``
    credential reference (ADR-013 section 3) — "rotation" from this table's
    perspective, since the secret value itself lives outside the database.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        kind: str,
        display_name: str,
        external_subject: str | None = None,
        external_issuer: str | None = None,
        credential_ref: str | None = None,
        id: str | None = None,  # noqa: A002 - matches the column name deliberately
    ) -> Principal:
        principal = Principal(
            id=id or new_ulid(),
            kind=kind,
            display_name=display_name,
            external_issuer=external_issuer,
            credential_ref=credential_ref,
            external_subject=external_subject,
        )
        self._session.add(principal)
        self._session.flush()
        return principal

    def get(self, principal_id: str) -> Principal | None:
        return self._session.get(Principal, principal_id)

    def get_by_external_identity(self, *, issuer: str, subject: str) -> Principal | None:
        """The JIT-provisioning lookup: does a principal already exist for this
        ``(iss, sub)`` pair. Never matches on ``subject`` alone (ADR-014)."""
        stmt: Select[tuple[Principal]] = select(Principal).where(
            Principal.external_issuer == issuer, Principal.external_subject == subject
        )
        return self._session.scalars(stmt).one_or_none()

    def disable(self, principal_id: str, *, disabled_at: datetime | None = None) -> Principal:
        principal = self._session.get(Principal, principal_id)
        if principal is None:
            raise LookupError(f"no such principal: {principal_id}")
        principal.disabled_at = disabled_at or utc_now()
        self._session.flush()
        return principal

    def enable(self, principal_id: str) -> Principal:
        principal = self._session.get(Principal, principal_id)
        if principal is None:
            raise LookupError(f"no such principal: {principal_id}")
        principal.disabled_at = None
        self._session.flush()
        return principal

    def set_credential_ref(self, principal_id: str, credential_ref: str | None) -> Principal:
        principal = self._session.get(Principal, principal_id)
        if principal is None:
            raise LookupError(f"no such principal: {principal_id}")
        principal.credential_ref = credential_ref
        self._session.flush()
        return principal

    def list_service_principals(self, *, include_disabled: bool = True) -> list[Principal]:
        stmt: Select[tuple[Principal]] = select(Principal).where(Principal.kind == "service")
        if not include_disabled:
            stmt = stmt.where(Principal.disabled_at.is_(None))
        return list(self._session.scalars(stmt.order_by(Principal.created_at)))


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


class WorkflowDefinitionSnapshotRepository:
    """The ``workflow_definition_snapshots`` table (stage 07, ADR-008) —
    ``diff_workflow_definition``'s "registered" side. ``create`` is a get-or-create:
    re-capturing an already-stored ``(workflow_id, definition_hash)`` pair is a no-op,
    not a duplicate — the caller (``registry hash --n8n-workflow-id``) never needs to
    check first."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, workflow_id: str, definition_hash: str) -> WorkflowDefinitionSnapshot | None:
        stmt: Select[tuple[WorkflowDefinitionSnapshot]] = select(WorkflowDefinitionSnapshot).where(
            WorkflowDefinitionSnapshot.workflow_id == workflow_id,
            WorkflowDefinitionSnapshot.definition_hash == definition_hash,
        )
        return self._session.scalars(stmt).one_or_none()

    def create(
        self,
        *,
        workflow_id: str,
        definition_hash: str,
        canonical_definition: dict[str, Any],
        captured_by: str,
        id: str | None = None,  # noqa: A002
    ) -> WorkflowDefinitionSnapshot:
        existing = self.get(workflow_id=workflow_id, definition_hash=definition_hash)
        if existing is not None:
            return existing
        snapshot = WorkflowDefinitionSnapshot(
            id=id or new_ulid(),
            workflow_id=workflow_id,
            definition_hash=definition_hash,
            canonical_definition=canonical_definition,
            captured_by=captured_by,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot


def _operation_scope_clauses(
    *,
    workflow_id_like_patterns: builtins.list[str] | None,
    environment: str | None,
    since: datetime | None,
) -> builtins.list[Any]:
    """The where-clauses ``get_metrics`` (stage 08, ADR-019) shares with
    ``OperationRepository.list``'s own scope-filtering shape: ``None`` patterns means
    no restriction (v1, or a v2 caller not yet scope-resolved by the caller); an empty
    (non-``None``) list matches nothing, by construction, exactly like ``list`` already
    guarantees. Module-level (not a method) so :class:`ExecutionResultRepository` can
    apply the identical scope to its own join without reaching into
    ``OperationRepository``'s internals."""
    clauses: builtins.list[Any] = []
    if workflow_id_like_patterns is not None:
        if not workflow_id_like_patterns:
            clauses.append(false())
        else:
            clauses.append(
                or_(
                    *(
                        Operation.workflow_id.like(pattern, escape="\\")
                        for pattern in workflow_id_like_patterns
                    )
                )
            )
    if environment is not None:
        clauses.append(Operation.environment == environment)
    if since is not None:
        clauses.append(Operation.created_at >= since)
    return clauses


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
        organization_id: str | None = None,
        environment_id: str | None = None,
        parent_operation_id: str | None = None,
    ) -> Operation:
        """Insert a new operation row at ``state_version=1``.

        Does not append an event row for this creation — callers that want the T01 event
        recorded atomically alongside creation should do so explicitly in the same
        session (this mirrors :meth:`apply_transition`, which never creates the row it
        transitions, only ever updates one that already exists).

        ``parent_operation_id`` (stage 06, ADR-012 section 1) is set only by
        ``core.service.retry_operation`` — ``None`` for every ordinary
        ``prepare_operation`` call, exactly as it always has been.
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
            organization_id=organization_id,
            environment_id=environment_id,
            parent_operation_id=parent_operation_id,
        )
        self._session.add(operation)
        self._session.flush()
        return operation

    def get(self, operation_id: str) -> Operation | None:
        return self._session.get(Operation, operation_id)

    def get_for_update(self, operation_id: str) -> Operation | None:
        """As :meth:`get`, but with a row lock (``SELECT ... FOR UPDATE``) held for
        the rest of the caller's transaction — stage 05's own concurrency need: two
        genuinely concurrent quorum decisions on the same operation must serialize
        around reading-then-tallying its ``Approval`` rows, or each could read the
        other's not-yet-committed vote as absent and neither would ever see quorum
        reached (a lost-update race distinct from, and in addition to, the
        ``state_version`` compare-and-set :meth:`compare_and_set_state` already
        guards for the T06/T07 transition itself). A plain no-op serialization
        boundary on SQLite (ADR-004 rule D4 portability — SQLite's single-writer
        model already serializes every write, so ``FOR UPDATE`` there adds nothing
        but costs nothing either), a real row lock on PostgreSQL.
        """
        stmt: Select[tuple[Operation]] = (
            select(Operation).where(Operation.id == operation_id).with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()

    def find_by_idempotency(
        self, *, principal_id: str, environment: str, workflow_id: str, idempotency_key: str
    ) -> Operation | None:
        """Look up an operation by its full idempotency namespace (ADR-011).

        Only ever called with a non-``None`` key: two rows sharing a namespace with no
        key set are never duplicates of each other (see the module docstring on
        ``storage/models.py``), so there is nothing meaningful to "find" by a null key.

        A retry's own idempotency key is scoped to its parent by ``core.service.
        _prepare_or_retry`` folding the parent's ID into the ``idempotency_key`` value
        itself before it ever reaches this method (see ``storage/models.py``'s
        ``Operation`` docstring for why — not by an extra parameter here).
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
        principal_id: str | None,
        environment: str | None = None,
        workflow_id: str | None = None,
        workflow_id_like_patterns: builtins.list[str] | None = None,
        states: builtins.list[str] | None = None,
        since: datetime | None = None,
        limit: int = 20,
        before_id: str | None = None,
    ) -> list[Operation]:
        """Filtered, most-recent-first history (MCP_TOOLS.md 2.10).

        ``principal_id=None`` means "any principal" — v2 role-based visibility (ADR-015)
        is scope-based, not ownership-based, so a caller whose grants cover a workflow
        can see every principal's operations against it, not only their own;
        ``core.service.list_operations`` passes ``None`` only when ``enable_v2`` and the
        caller's own scope has already been resolved into ``workflow_id_like_patterns``.
        ``workflow_id_like_patterns`` are pre-translated SQL ``LIKE`` patterns (see
        ``core.authorization.workflow_scope_to_sql_like`` — this module does no glob
        translation itself, staying storage-only per ADR-001) OR'd together with
        ``ESCAPE '\\'``; a call with an empty (non-``None``) list matches nothing, by
        SQL construction, rather than accidentally matching everything.

        Filtering — including the scope filters above — is applied **before** ``LIMIT``,
        never after: a cursor must never walk past a row a filter would have hidden, or
        a page could come back short of a caller-visible row that exists beyond the
        page boundary (the "pagination side channel" Stage 03's completion gate names).

        ``before_id`` pages backward through the same ordering: operation IDs are
        ``op_<ULID>``, lexicographically sortable the same way ``created_at`` is, so
        "strictly older than the last row of the previous page" is ``id < before_id``
        without a second sort key or an offset that shifts under concurrent inserts.

        Applies no other policy of its own (per the module docstring) — including no
        lazy expiry, which is a state-machine concern; a caller that needs every
        returned row current must apply it itself, per row, after this query returns.
        """
        stmt: Select[tuple[Operation]] = select(Operation)
        if principal_id is not None:
            stmt = stmt.where(Operation.principal_id == principal_id)
        if environment is not None:
            stmt = stmt.where(Operation.environment == environment)
        if workflow_id is not None:
            stmt = stmt.where(Operation.workflow_id == workflow_id)
        if workflow_id_like_patterns is not None:
            if not workflow_id_like_patterns:
                stmt = stmt.where(false())  # deliberately unsatisfiable
            else:
                stmt = stmt.where(
                    or_(
                        *(
                            Operation.workflow_id.like(pattern, escape="\\")
                            for pattern in workflow_id_like_patterns
                        )
                    )
                )
        if states:
            stmt = stmt.where(Operation.state.in_(states))
        if since is not None:
            stmt = stmt.where(Operation.created_at >= since)
        if before_id is not None:
            stmt = stmt.where(Operation.id < before_id)
        stmt = stmt.order_by(Operation.created_at.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def list_all(self, *, limit: int = 10_000) -> builtins.list[Operation]:
        """Every operation, across every principal, oldest first — audit export's own
        read path (phase 8, AC-25). Not principal-scoped, unlike :meth:`list`: an
        export is an operator-level view of the whole system, not one caller's own
        history."""
        stmt: Select[tuple[Operation]] = (
            select(Operation).order_by(Operation.created_at.asc()).limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_overdue(self, *, now: datetime) -> builtins.list[Operation]:
        """Every operation, across every principal, whose deadline has passed while it
        sits in a state lazy expiry would move (``PENDING_APPROVAL`` past
        ``approval_expires_at``, or ``APPROVED`` past ``execution_deadline`` — T08/T11,
        ADR-010). Not principal-scoped: this is what a system-wide maintenance sweep
        (``operations expire``, the approval app's best-effort sweeper) needs, unlike
        :meth:`list`, which is always one principal's own history.

        Return type is spelled ``builtins.list`` rather than bare ``list``: this class
        already defines a method named ``list``, which shadows the builtin in this
        class's namespace for annotation resolution (``from __future__ import
        annotations`` makes every annotation here a forward reference, resolved against
        this scope).
        """
        stmt: Select[tuple[Operation]] = select(Operation).where(
            (
                (Operation.state == "PENDING_APPROVAL")
                & (Operation.approval_expires_at.is_not(None))
                & (Operation.approval_expires_at < now)
            )
            | (
                (Operation.state == "APPROVED")
                & (Operation.execution_deadline.is_not(None))
                & (Operation.execution_deadline < now)
            )
        )
        return list(self._session.scalars(stmt))

    def count_recent(self, *, workflow_id: str, since: datetime) -> int:
        """How many operations for ``workflow_id`` — across every principal — were
        created at or after ``since`` (phase 7's ``rate_limit_per_minute``,
        MCP_TOOLS.md section 2.6). Rate limiting is a property of the *workflow*, not
        of one principal's own history, so this is deliberately not scoped to a
        principal the way :meth:`list` is."""
        stmt = (
            select(func.count())
            .select_from(Operation)
            .where(Operation.workflow_id == workflow_id, Operation.created_at >= since)
        )
        return self._session.scalar(stmt) or 0

    def count_in_states(self, *, workflow_id: str, states: builtins.list[str]) -> int:
        """How many operations for ``workflow_id`` currently sit in one of ``states`` —
        phase 7's ``max_concurrent`` check (MCP_TOOLS.md section 2.8), evaluated at
        execute time against the live count of ``EXECUTING`` operations. Not
        principal-scoped, for the same reason as :meth:`count_recent`."""
        stmt = (
            select(func.count())
            .select_from(Operation)
            .where(Operation.workflow_id == workflow_id, Operation.state.in_(states))
        )
        return self._session.scalar(stmt) or 0

    def count_by_outcome(
        self,
        *,
        workflow_id_like_patterns: builtins.list[str] | None,
        environment: str | None,
        since: datetime | None,
    ) -> dict[str, int]:
        """``{state: count}`` for every state present, scoped and windowed
        identically to :meth:`list` (``get_metrics``'s ``totals.by_outcome``, ADR-019
        section 2 — filtered before aggregation, never after)."""
        clauses = _operation_scope_clauses(
            workflow_id_like_patterns=workflow_id_like_patterns,
            environment=environment,
            since=since,
        )
        stmt = select(Operation.state, func.count()).group_by(Operation.state)
        if clauses:
            stmt = stmt.where(*clauses)
        return dict(cast("list[tuple[str, int]]", self._session.execute(stmt).all()))

    def breakdown_by_workflow(
        self,
        *,
        workflow_id_like_patterns: builtins.list[str] | None,
        environment: str | None,
        since: datetime | None,
    ) -> builtins.list[tuple[str, int, dict[str, int]]]:
        """``[(workflow_id, count, {state: count})]``, most-frequent workflow first —
        ``get_metrics``'s ``group_by=workflow`` breakdown (ADR-019 section 3). Every
        distinct workflow present in the window is returned; cardinality here is
        bounded by the *registry's own* distinct workflow count (a caller can create
        operations, never new workflow ids), so the 50-entry cap and ``"other"`` fold
        are the caller's (``core.service.get_metrics``'s) job, not this query's."""
        clauses = _operation_scope_clauses(
            workflow_id_like_patterns=workflow_id_like_patterns,
            environment=environment,
            since=since,
        )
        by_state_stmt = select(Operation.workflow_id, Operation.state, func.count()).group_by(
            Operation.workflow_id, Operation.state
        )
        if clauses:
            by_state_stmt = by_state_stmt.where(*clauses)
        totals: dict[str, dict[str, int]] = {}
        for workflow_id, state, count in self._session.execute(by_state_stmt).all():
            totals.setdefault(workflow_id, {})[state] = count
        rows = [
            (workflow_id, sum(by_outcome.values()), by_outcome)
            for workflow_id, by_outcome in totals.items()
        ]
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows

    def stuck_executing(
        self, *, older_than: datetime, limit: int = 500
    ) -> builtins.list[Operation]:
        """Every ``EXECUTING`` operation last updated before ``older_than`` (stage 08's
        alert-hook sweep, BUILD_PLAN section 8's "EXECUTING stuck past threshold").
        Mirrors :meth:`list_overdue`'s own shape — a system-wide sweep, not
        principal-scoped."""
        stmt: Select[tuple[Operation]] = (
            select(Operation)
            .where(Operation.state == "EXECUTING", Operation.updated_at < older_than)
            .order_by(Operation.updated_at.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_unknown(self, *, limit: int = 500) -> builtins.list[Operation]:
        """Every operation currently ``UNKNOWN`` (stage 08's alert-hook sweep).
        Re-scanned on every sweep tick, like :meth:`list_overdue` — the alert-hook's
        own permanent per-``(subject_id, event_type)`` delivery dedup
        (``core.service._deliver_with_dedup``) is what keeps a re-scan from re-alerting,
        not this query only returning "new" rows."""
        stmt: Select[tuple[Operation]] = (
            select(Operation)
            .where(Operation.state == "UNKNOWN")
            .order_by(Operation.updated_at.asc())
            .limit(limit)
        )
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
        binding_hash: str,
        expires_at: datetime,
        quorum_count: int = 1,
        assigned_to: str | None = None,
        id: str | None = None,  # noqa: A002
    ) -> Approval:
        approval = Approval(
            id=id or new_ulid(),
            operation_id=operation_id,
            token_hash=token_hash,
            binding_hash=binding_hash,
            expires_at=expires_at,
            quorum_count=quorum_count,
            assigned_to=assigned_to,
        )
        self._session.add(approval)
        self._session.flush()
        return approval

    def get_by_token_hash(self, token_hash: str) -> Approval | None:
        stmt: Select[tuple[Approval]] = select(Approval).where(Approval.token_hash == token_hash)
        return self._session.scalars(stmt).one_or_none()

    def get_by_operation_id(self, operation_id: str) -> Approval | None:
        """The approval row minted for ``operation_id`` at T04, if this operation ever
        entered ``PENDING_APPROVAL`` (an auto-approved T05 operation has none). v1
        only — assumes exactly one row per operation, which is no longer true once
        stage 05's quorum mode mints one row per eligible approver; a v2 call site
        wants :meth:`list_for_operation` or :meth:`get_by_operation_and_decider`
        instead."""
        stmt: Select[tuple[Approval]] = select(Approval).where(
            Approval.operation_id == operation_id
        )
        return self._session.scalars(stmt).one_or_none()

    def list_for_operation(self, operation_id: str) -> builtins.list[Approval]:
        """Every approval row for one operation — v1 has exactly one; v2 quorum mode
        has one per eligible approver who has been minted a row (via
        ``request_approval`` or a direct CLI decision). Ordered by ``issued_at`` for
        a stable, reproducible tally/display order."""
        stmt: Select[tuple[Approval]] = (
            select(Approval)
            .where(Approval.operation_id == operation_id)
            .order_by(Approval.issued_at)
        )
        return list(self._session.scalars(stmt))

    def get_by_operation_and_decider(self, operation_id: str, principal_id: str) -> Approval | None:
        """This principal's own row for this operation — decided or still pending —
        matched by ``assigned_to`` (minted via ``request_approval``) or by
        ``decided_by`` (already decided, however the row was minted). Stage 05's own
        "have I already got a slot / have I already decided" check."""
        stmt: Select[tuple[Approval]] = select(Approval).where(
            Approval.operation_id == operation_id,
            or_(Approval.assigned_to == principal_id, Approval.decided_by == principal_id),
        )
        return self._session.scalars(stmt).one_or_none()

    def record_decision(
        self,
        *,
        approval_id: str,
        decision: str,
        decided_by: str,
        decided_at: datetime | None = None,
        client_fingerprint: str | None = None,
    ) -> Approval:
        approval = self._session.get(Approval, approval_id)
        if approval is None:
            raise LookupError(f"no such approval: {approval_id}")
        approval.decision = decision
        approval.decided_by = decided_by
        approval.decided_at = decided_at or utc_now()
        if client_fingerprint is not None:
            approval.client_fingerprint = client_fingerprint
        self._session.flush()
        return approval


class NotificationDeliveryRepository:
    """The ``notification_deliveries`` table (ADR-018) — one row per distinct
    idempotency key (``(subject_id, principal_id, event_type)``), tracking bounded
    retry state for approval-routing and (stage 08) alert-hook notifications alike."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_idempotency_key(self, idempotency_key: str) -> NotificationDelivery | None:
        stmt: Select[tuple[NotificationDelivery]] = select(NotificationDelivery).where(
            NotificationDelivery.idempotency_key == idempotency_key
        )
        return self._session.scalars(stmt).one_or_none()

    def create(
        self,
        *,
        idempotency_key: str,
        subject_type: str,
        subject_id: str,
        event_type: str,
        principal_id: str | None = None,
        id: str | None = None,  # noqa: A002
    ) -> NotificationDelivery:
        delivery = NotificationDelivery(
            id=id or new_ulid(),
            idempotency_key=idempotency_key,
            subject_type=subject_type,
            subject_id=subject_id,
            principal_id=principal_id,
            event_type=event_type,
            attempts=0,
            status="pending",
        )
        self._session.add(delivery)
        self._session.flush()
        return delivery

    def record_attempt(
        self, delivery_id: str, *, delivered: bool, attempted_at: datetime | None = None
    ) -> NotificationDelivery:
        delivery = self._session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            raise LookupError(f"no such notification delivery: {delivery_id}")
        now = attempted_at or utc_now()
        delivery.attempts += 1
        delivery.last_attempted_at = now
        if delivered:
            delivery.status = "delivered"
            delivery.delivered_at = now
        self._session.flush()
        return delivery

    def mark_failed(self, delivery_id: str) -> NotificationDelivery:
        """Bounded retry exhausted — fail-visible, never retried again (ADR-018 §2)."""
        delivery = self._session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            raise LookupError(f"no such notification delivery: {delivery_id}")
        delivery.status = "failed"
        self._session.flush()
        return delivery

    def list_pending(self) -> builtins.list[NotificationDelivery]:
        stmt: Select[tuple[NotificationDelivery]] = (
            select(NotificationDelivery)
            .where(NotificationDelivery.status == "pending")
            .order_by(NotificationDelivery.attempts, NotificationDelivery.last_attempted_at)
        )
        return list(self._session.scalars(stmt))


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

    def list_finished_durations_ms(
        self,
        *,
        workflow_id_like_patterns: builtins.list[str] | None,
        environment: str | None,
        since: datetime | None,
    ) -> builtins.list[float]:
        """``(finished_at - started_at)`` in milliseconds for every finished execution
        in scope and window (``get_metrics``'s ``latency_ms``, ADR-019 section 4).
        Percentiles are computed in Python over this bounded, in-memory list — no
        dialect-specific percentile SQL function exists across SQLite and PostgreSQL
        (ADR-004), and the window bound (at most 30 days) keeps this list small enough
        to sort in memory. Joins to ``operations`` for scope and ``since`` (the window
        is defined by *when the operation was created*, matching every other
        ``get_metrics`` dimension), so an execution the caller isn't authorized to see
        is excluded before any duration is ever computed, not after."""
        clauses = [
            ExecutionResult.started_at.is_not(None),
            ExecutionResult.finished_at.is_not(None),
        ]
        stmt = select(ExecutionResult.started_at, ExecutionResult.finished_at).join(
            Operation, Operation.id == ExecutionResult.operation_id
        )
        op_scope = _operation_scope_clauses(
            workflow_id_like_patterns=workflow_id_like_patterns,
            environment=environment,
            since=since,
        )
        stmt = stmt.where(*clauses, *op_scope)
        durations: builtins.list[float] = []
        for started_at, finished_at in self._session.execute(stmt).all():
            durations.append((finished_at - started_at).total_seconds() * 1000)
        return durations


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

    def list_for_subject(self, *, subject_type: str, subject_id: str) -> list[AuditLogEntry]:
        """Every audit entry for one subject, oldest first — stage 06's
        ``operations reconcile list`` read path (uses ``ix_audit_log_subject``,
        migration 0006). Not filtered by ``action``; a caller wanting only
        reconciliation annotations (as opposed to every transition/denial recorded
        for the same subject) filters the result, the same "this repository has no
        opinion on meaning, only storage" discipline every other method here keeps."""
        stmt: Select[tuple[AuditLogEntry]] = (
            select(AuditLogEntry)
            .where(
                AuditLogEntry.subject_type == subject_type, AuditLogEntry.subject_id == subject_id
            )
            .order_by(AuditLogEntry.seq)
        )
        return list(self._session.scalars(stmt))

    def list_range(self, *, start_seq: int = 1, limit: int = 100) -> list[AuditLogEntry]:
        stmt: Select[tuple[AuditLogEntry]] = (
            select(AuditLogEntry)
            .where(AuditLogEntry.seq >= start_seq)
            .order_by(AuditLogEntry.seq)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_all(self, *, page_size: int = 500) -> list[AuditLogEntry]:
        """Every row in ``seq`` order, paging through :meth:`list_range` until a page
        comes back short — ``audit verify``/``audit export``'s own read path (AC-22,
        AC-25). Chain verification assumes the *first* entry it sees has
        ``prev_hash == GENESIS_HASH``; a caller that instead fetched one 100-row page
        at a time and verified each page independently would misreport every page
        after the first as broken, so this method exists to hand back the whole table
        in one call."""
        entries: list[AuditLogEntry] = []
        start_seq = 1
        while True:
            page = self.list_range(start_seq=start_seq, limit=page_size)
            if not page:
                break
            entries.extend(page)
            if len(page) < page_size:
                break
            start_seq = page[-1].seq + 1
        return entries

    def list_page(
        self,
        *,
        before_seq: int | None,
        limit: int,
        since: datetime | None,
        workflow_id: str | None,
        workflow_id_like_patterns: builtins.list[str] | None,
        environment_id: str | None,
        include_registry_snapshot_events: bool,
    ) -> list[AuditLogEntry]:
        """``list_audit_events``'s own read path (stage 08, ADR-012 section 3) — most
        recent first, ``seq < before_seq`` as the page boundary (mirrors
        ``OperationRepository.list``'s ``before_id <`` pattern, just descending on the
        integer ``seq`` instead of a lexicographically-sortable ULID string).

        Authorization filters the query, not the result (ADR-012 section 3): an entry
        is included only if its ``subject_type``/``subject_id`` resolves to something
        in scope —

        * ``subject_type="workflow"``: ``subject_id`` matches a pattern directly.
        * ``subject_type="operation"``: the referenced operation's own ``workflow_id``
          matches a pattern (a correlated ``EXISTS`` against ``operations``, not a join,
          so a matching operation never duplicates its audit rows).
        * ``subject_type="environment"``: ``subject_id == environment_id`` — the caller
          already only ever has a caller-visible ``environment_id`` to pass here
          (``identity.resolve_environment`` applied its own visibility rule before this
          point), so no further pattern check is needed for this branch.
        * ``subject_type="registry_snapshot"``: included only when
          ``include_registry_snapshot_events`` — a whole-registry-document event has no
          single workflow/environment owner to scope against, so "no scope" here means
          "admin only", never "visible to everyone" (the stage's own resolved design
          decision).

        ``workflow_id_like_patterns=None`` means no scope restriction at all (v1, which
        has no RBAC concept — every subject type is visible); a non-``None`` empty list
        makes the first two branches unsatisfiable by construction, exactly like
        ``OperationRepository.list`` already guarantees for operations themselves. A
        caller-supplied ``workflow_id`` filter further restricts to that one workflow's
        own events (both direct ``subject_type="workflow"`` rows and, via the same
        ``EXISTS``, that workflow's own operations), ANDed on top of the scope clause.
        """
        stmt: Select[tuple[AuditLogEntry]] = select(AuditLogEntry)

        if workflow_id_like_patterns is not None:
            if not workflow_id_like_patterns:
                workflow_clause: Any = false()
                operation_clause: Any = false()
            else:
                workflow_like = or_(
                    *(
                        AuditLogEntry.subject_id.like(pattern, escape="\\")
                        for pattern in workflow_id_like_patterns
                    )
                )
                operation_like = or_(
                    *(
                        Operation.workflow_id.like(pattern, escape="\\")
                        for pattern in workflow_id_like_patterns
                    )
                )
                workflow_clause = (AuditLogEntry.subject_type == "workflow") & workflow_like
                operation_clause = (AuditLogEntry.subject_type == "operation") & (
                    select(Operation.id)
                    .where(Operation.id == AuditLogEntry.subject_id, operation_like)
                    .exists()
                )
            environment_clause: Any = (AuditLogEntry.subject_type == "environment") & (
                AuditLogEntry.subject_id == environment_id if environment_id else false()
            )
            registry_clause: Any = (AuditLogEntry.subject_type == "registry_snapshot") & (
                true() if include_registry_snapshot_events else false()
            )
            stmt = stmt.where(
                or_(workflow_clause, operation_clause, environment_clause, registry_clause)
            )

        if workflow_id is not None:
            stmt = stmt.where(
                or_(
                    (AuditLogEntry.subject_type == "workflow")
                    & (AuditLogEntry.subject_id == workflow_id),
                    (AuditLogEntry.subject_type == "operation")
                    & (
                        select(Operation.id)
                        .where(
                            Operation.id == AuditLogEntry.subject_id,
                            Operation.workflow_id == workflow_id,
                        )
                        .exists()
                    ),
                )
            )

        if since is not None:
            stmt = stmt.where(AuditLogEntry.occurred_at >= since)
        if before_seq is not None:
            stmt = stmt.where(AuditLogEntry.seq < before_seq)

        stmt = stmt.order_by(AuditLogEntry.seq.desc()).limit(limit)
        return list(self._session.scalars(stmt))


class OrganizationRepository:
    """The ``organizations`` table — the v2 tenant boundary (ADR-013 section 1).

    No cross-organization query exists in the *MCP tool* surface (ADR-013's own
    stated boundary), but this is the storage layer the local admin CLI (stage 02) and
    ``whoami`` both need — an operator's own deployment inspecting its own database is
    not the cross-tenant query ADR-013 forecloses.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, name: str, id: str | None = None) -> Organization:  # noqa: A002
        organization = Organization(id=id or new_ulid(), name=name)
        self._session.add(organization)
        self._session.flush()
        return organization

    def get(self, organization_id: str) -> Organization | None:
        return self._session.get(Organization, organization_id)

    def list(self) -> list[Organization]:
        stmt: Select[tuple[Organization]] = select(Organization).order_by(Organization.created_at)
        return list(self._session.scalars(stmt))


class OrganizationMembershipRepository:
    """The ``organization_memberships`` table — the RBAC grant (ADR-013 section 2,
    ADR-015). ``active_organization_id`` is kept in lock-step with ``removed_at`` by
    this class alone (never set directly by a caller): equal to ``organization_id``
    while active, ``NULL`` once :meth:`remove` is called — the portable NULL-uniqueness
    technique ``uq_organization_memberships_active`` relies on (see
    ``storage/models.py``'s ``OrganizationMembership`` docstring).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        principal_id: str,
        organization_id: str,
        roles: builtins.list[str],
        workflow_scope: str = "*",
        environment_scope: builtins.list[str] | None = None,
        id: str | None = None,  # noqa: A002
    ) -> OrganizationMembership:
        if not roles:
            raise ValueError("a membership must grant at least one role")
        membership = OrganizationMembership(
            id=id or new_ulid(),
            principal_id=principal_id,
            organization_id=organization_id,
            active_organization_id=organization_id,
            roles=list(roles),
            workflow_scope=workflow_scope,
            environment_scope=list(environment_scope) if environment_scope else ["*"],
        )
        self._session.add(membership)
        self._session.flush()
        return membership

    def get_active(
        self, *, principal_id: str, organization_id: str
    ) -> OrganizationMembership | None:
        stmt: Select[tuple[OrganizationMembership]] = select(OrganizationMembership).where(
            OrganizationMembership.principal_id == principal_id,
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.removed_at.is_(None),
        )
        return self._session.scalars(stmt).one_or_none()

    def list_active_for_principal(self, principal_id: str) -> builtins.list[OrganizationMembership]:
        """Every organization this principal currently belongs to — ``whoami``'s own
        read path. Queried fresh on every call; nothing here is cached (ADR-014
        section 4's "re-read, never cache" discipline extended from disabled
        principals to memberships)."""
        stmt: Select[tuple[OrganizationMembership]] = (
            select(OrganizationMembership)
            .where(
                OrganizationMembership.principal_id == principal_id,
                OrganizationMembership.removed_at.is_(None),
            )
            .order_by(OrganizationMembership.created_at)
        )
        return list(self._session.scalars(stmt))

    def list_active_for_organization(
        self, organization_id: str
    ) -> builtins.list[OrganizationMembership]:
        """Every active membership *in* one organization, across every principal —
        the complement of :meth:`list_active_for_principal` (one principal, every
        org). Stage 05's own read path: computing an operation's eligible-approver
        snapshot means enumerating every member of the operation's organization, not
        one principal's own memberships. Queried fresh, never cached, for the same
        reason every other membership read here is."""
        stmt: Select[tuple[OrganizationMembership]] = (
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.removed_at.is_(None),
            )
            .order_by(OrganizationMembership.created_at)
        )
        return list(self._session.scalars(stmt))

    def list_for_organization(
        self, organization_id: str, *, include_removed: bool = False
    ) -> builtins.list[OrganizationMembership]:
        stmt: Select[tuple[OrganizationMembership]] = select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id
        )
        if not include_removed:
            stmt = stmt.where(OrganizationMembership.removed_at.is_(None))
        return list(self._session.scalars(stmt.order_by(OrganizationMembership.created_at)))

    def remove(self, membership_id: str) -> OrganizationMembership:
        membership = self._session.get(OrganizationMembership, membership_id)
        if membership is None:
            raise LookupError(f"no such membership: {membership_id}")
        membership.removed_at = utc_now()
        membership.active_organization_id = None
        self._session.flush()
        return membership


class EnvironmentRepository:
    """The ``environments`` table (ADR-016). Full CRUD as of stage 04 — ``create``/
    ``get``/``archive`` alongside the read-only ``list_for_organization`` stage 02
    already added for ``whoami``. Archival only (``archived_at``, never a row delete —
    ADR-016 section 4): historical operations must stay resolvable against an
    environment an organization has since retired.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        organization_id: str,
        name: str,
        n8n_base_url_ref: str,
        n8n_api_key_ref: str,
        is_production: bool = False,
        id: str | None = None,  # noqa: A002
    ) -> Environment:
        environment = Environment(
            id=id or new_ulid(),
            organization_id=organization_id,
            name=name,
            n8n_base_url_ref=n8n_base_url_ref,
            n8n_api_key_ref=n8n_api_key_ref,
            is_production=is_production,
        )
        self._session.add(environment)
        self._session.flush()
        return environment

    def get(self, environment_id: str) -> Environment | None:
        return self._session.get(Environment, environment_id)

    def archive(self, environment_id: str) -> Environment:
        environment = self._session.get(Environment, environment_id)
        if environment is None:
            raise LookupError(f"no such environment: {environment_id}")
        environment.archived_at = utc_now()
        self._session.flush()
        return environment

    def list_for_organization(
        self, organization_id: str, *, include_archived: bool = False
    ) -> builtins.list[Environment]:
        stmt: Select[tuple[Environment]] = select(Environment).where(
            Environment.organization_id == organization_id
        )
        if not include_archived:
            stmt = stmt.where(Environment.archived_at.is_(None))
        return list(self._session.scalars(stmt.order_by(Environment.created_at)))


class WorkflowEnvironmentOverlayRepository:
    """The ``workflow_environment_overlays`` table (ADR-016, rules R13-R14). Rows are
    deliberately mutable — the one exception to this codebase's usual "insert once,
    never update" storage discipline (``registry_snapshots``, ``workflow_bindings``,
    ``operation_events``, ``audit_log`` are all append-only): an overlay is a *current
    policy setting* for a workflow in an environment, not a historical record. What
    must never change retroactively is what an already-*prepared* operation was
    actually governed by — that guarantee comes from ``core.service`` resolving and
    freezing the merged contract once, at ``prepare_operation`` time, onto the
    operation's own row (``operations.definition_hash`` etc.), never from re-reading
    this table later. See ``registry/schema.py``'s ``resolve_overlay`` docstring.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        workflow_id: str,
        environment_id: str,
        n8n_workflow_id: str | None = None,
        definition_hash: str | None = None,
        trigger_path: str | None = None,
        trigger_secret_ref: str | None = None,
        approval_override: str | None = None,
        limits_override: dict[str, int] | None = None,
    ) -> WorkflowEnvironmentOverlay:
        existing = self.get(workflow_id, environment_id)
        if existing is not None:
            existing.n8n_workflow_id = n8n_workflow_id
            existing.definition_hash = definition_hash
            existing.trigger_path = trigger_path
            existing.trigger_secret_ref = trigger_secret_ref
            existing.approval_override = approval_override
            existing.limits_override = limits_override
            self._session.flush()
            return existing
        overlay = WorkflowEnvironmentOverlay(
            id=new_ulid(),
            workflow_id=workflow_id,
            environment_id=environment_id,
            n8n_workflow_id=n8n_workflow_id,
            definition_hash=definition_hash,
            trigger_path=trigger_path,
            trigger_secret_ref=trigger_secret_ref,
            approval_override=approval_override,
            limits_override=limits_override,
        )
        self._session.add(overlay)
        self._session.flush()
        return overlay

    def get(self, workflow_id: str, environment_id: str) -> WorkflowEnvironmentOverlay | None:
        stmt: Select[tuple[WorkflowEnvironmentOverlay]] = select(WorkflowEnvironmentOverlay).where(
            WorkflowEnvironmentOverlay.workflow_id == workflow_id,
            WorkflowEnvironmentOverlay.environment_id == environment_id,
        )
        return self._session.scalars(stmt).one_or_none()

    def list_for_environment(
        self, environment_id: str
    ) -> builtins.list[WorkflowEnvironmentOverlay]:
        stmt: Select[tuple[WorkflowEnvironmentOverlay]] = select(WorkflowEnvironmentOverlay).where(
            WorkflowEnvironmentOverlay.environment_id == environment_id
        )
        return list(self._session.scalars(stmt.order_by(WorkflowEnvironmentOverlay.workflow_id)))

    def delete(self, workflow_id: str, environment_id: str) -> None:
        """Remove one environment's overlay for one workflow — a real deletion (unlike
        every append-only table in this module), matching this table's own "current
        policy setting, not a historical record" nature (see this class's docstring).
        A no-op if no such row exists."""
        existing = self.get(workflow_id, environment_id)
        if existing is not None:
            self._session.delete(existing)
            self._session.flush()


__all__ = [
    "ApprovalRepository",
    "AuditLogRepository",
    "EnvironmentRepository",
    "ExecutionResultRepository",
    "NotificationDeliveryRepository",
    "OperationEventRepository",
    "OperationRepository",
    "OrganizationMembershipRepository",
    "OrganizationRepository",
    "PrincipalRepository",
    "RegistrySnapshotRepository",
    "WorkflowBindingRepository",
    "WorkflowDefinitionSnapshotRepository",
    "WorkflowEnvironmentOverlayRepository",
]
