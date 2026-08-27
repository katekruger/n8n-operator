"""Use-case orchestration — the portable core (ADR-001).

Exposes prepare / approve / execute / cancel / inspect as functions over plain domain
types (``core/models.py``). Every adapter calls into here; none of them reimplements
policy. Request flows are diagrammed in ``docs/ARCHITECTURE.md`` section 4.

Every function here takes a ``Session`` a caller already opened (via
``storage.session.session_scope``) and never commits it — a use case that performs
several writes (create the operation row, apply a transition, write the audit entry)
does so inside the caller's single transaction, so a failure partway through leaves
nothing behind rather than a partially-governed record (invariant I6). Every function
returns a **detached** domain object (``core.models.Operation``, not the SQLAlchemy row)
so a caller never needs a live session to read the result.

Registry use cases (Phase 2) and the operation lifecycle (Phase 3) live in the same
module because both need the one thing only ``core/`` is allowed to depend on both of at
once: ``registry/`` and ``storage/`` (ARCHITECTURE.md section 2.1).

Phase 3 adds the operation lifecycle, the state machine, redaction, and the audit trail.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

from n8n_operator.audit import writer as audit_writer
from n8n_operator.core import state_machine
from n8n_operator.core.handles import (
    compute_approval_binding,
    hash_approval_token,
    mint_approval_token,
    mint_operation_handle,
)
from n8n_operator.core.idempotency import (
    IdempotencyResolution,
    canonicalize_arguments,
    check_argument_size,
    fingerprint_arguments,
    resolve_idempotency,
)
from n8n_operator.core.models import (
    ApprovalDecisionContext,
    ExecutionResult,
    HealthCheckResult,
    Operation,
    PreflightResult,
    WorkflowContract,
)
from n8n_operator.core.redaction import cap_output, redact, scrub_secrets
from n8n_operator.errors import (
    ApprovalNotPendingError,
    ApprovalRequiredError,
    ApprovalTokenAlreadyUsedError,
    ApprovalTokenInvalidError,
    ArgumentMismatchError,
    ArgumentsTooLargeError,
    DefinitionDriftError,
    HandleAlreadyUsedError,
    HandleInvalidError,
    InvalidArgumentsError,
    InvalidStateTransitionError,
    OperationCanceledError,
    OperationExpiredError,
    OperationNotFoundError,
    OptimisticLockError,
    RegistryUnavailableError,
    ResultNotAvailableError,
    WorkflowDisabledError,
    WorkflowNotFoundError,
)
from n8n_operator.registry.loader import LoadedRegistry, load_registry
from n8n_operator.registry.schema import (
    RegistryDocument,
    WorkflowDetail,
    WorkflowEntry,
    WorkflowSummary,
)
from n8n_operator.registry.validation import ArgumentError, validate_arguments
from n8n_operator.storage.models import STATES, RegistrySnapshot
from n8n_operator.storage.models import Approval as ApprovalRow
from n8n_operator.storage.models import ExecutionResult as ExecutionResultRow
from n8n_operator.storage.models import Operation as OperationRow
from n8n_operator.storage.repository import (
    ApprovalRepository,
    AuditLogRepository,
    ExecutionResultRepository,
    OperationEventRepository,
    OperationRepository,
    RegistrySnapshotRepository,
    WorkflowBindingRepository,
)

__all__ = [
    "HealthPort",
    "PreflightPort",
    "approve_operation",
    "cancel_operation",
    "describe_workflow",
    "execute_operation",
    "expire_overdue_operations",
    "get_active_snapshot",
    "get_approval_decision_context",
    "get_execution_result",
    "get_instance_health",
    "get_operation",
    "list_operations",
    "list_workflows",
    "preflight_workflow",
    "prepare_operation",
    "record_execution_outcome",
    "reject_operation",
    "reload_registry",
    "resolve_approval_token",
    "validate_input",
    "validate_registry",
]


class PreflightPort(Protocol):
    """What ``prepare_operation`` (and the standalone ``preflight_workflow`` use case)
    need from a preflight checker.

    Phase 4's real ``n8n/preflight.py`` implements this against a live n8n instance.
    Phase 3 has no such adapter — every test here supplies a fake — which is exactly what
    this seam is for (ADR-001: the core depends on an interface, never on ``n8n/``
    directly, so it can be exercised, and its callers tested, without a network in the
    loop).
    """

    def check(self, workflow: WorkflowContract) -> PreflightResult: ...


class HealthPort(Protocol):
    """What ``get_instance_health`` needs from a reachability checker.

    Same seam as :class:`PreflightPort` for the same reason: Phase 5's MCP adapter
    never calls ``n8n/`` directly (ARCHITECTURE.md section 2.1 — an adapter "must not
    ... call n8n"); it calls ``core.service.get_instance_health``, which calls this
    port. The composition root (``mcp/server.py``) injects the real
    ``n8n.health.N8nHealth`` adapter; tests inject a fake.
    """

    def check(self) -> HealthCheckResult: ...


# ----------------------------------------------------------------------------------
# Transition bookkeeping — every state change writes both an ``operation_events`` row
# (via ``OperationRepository.apply_transition``) and an ``audit_log`` row (via
# ``audit/writer.py``), in the caller's transaction (invariant I6). These two tables
# translate a transition ID into the vocabulary each of those rows uses.
# ----------------------------------------------------------------------------------

_TRANSITION_ACTION: dict[str, str] = {
    "T01": "operation.prepared",
    "T02": "operation.invalid",
    "T03": "operation.blocked",
    "T04": "operation.pending_approval",
    "T05": "operation.auto_approved",
    "T06": "operation.approved",
    "T07": "operation.rejected",
    "T08": "operation.expired",
    "T09": "operation.canceled",
    "T10": "operation.execution_started",
    "T11": "operation.expired",
    "T12": "operation.canceled",
    "T13": "operation.succeeded",
    "T14": "operation.failed",
    "T15": "operation.indeterminate",
}

_TRANSITION_OUTCOME: dict[str, str] = {
    "T01": "allowed",
    "T02": "denied",
    "T03": "denied",
    "T04": "allowed",
    "T05": "allowed",
    "T06": "allowed",
    "T07": "denied",
    "T08": "denied",
    "T09": "denied",
    "T10": "allowed",
    "T11": "denied",
    "T12": "denied",
    "T13": "allowed",
    "T14": "error",
    "T15": "error",
}


def _apply_and_audit(
    session: Session,
    operation_row: OperationRow,
    transition_id: str,
    *,
    actor: str,
    detail: dict[str, Any] | None = None,
    **field_updates: Any,
) -> OperationRow:
    """Validate, apply, and audit one transition on an *existing* row.

    Not used for T01 (:func:`_record_creation`) — that transition creates the row rather
    than moving one, so there is no prior ``state_version`` to compare-and-set against.

    Race-safe against a concurrent writer that moved this row between our read and our
    write (two channels racing to decide the same operation — CLI and web page, or two
    web tabs; ADR-010's "both approval channels" are only safe together because of
    this): a lost compare-and-set is re-validated against the row's *current* state
    rather than left to leak a bare storage-layer :class:`OptimisticLockError` past
    ``core/`` — the loser gets the honest, taxonomy-mapped
    :class:`~n8n_operator.errors.InvalidStateTransitionError` naming what the operation
    actually is now, exactly as if it had read that state to begin with.
    """
    transition = state_machine.validate_transition(transition_id, from_state=operation_row.state)
    resolved_detail = detail or {}
    try:
        updated, _event = OperationRepository(session).apply_transition(
            operation_id=operation_row.id,
            expected_version=operation_row.state_version,
            new_state=transition.to_state,
            transition=transition_id,
            from_state=operation_row.state,
            actor=actor,
            detail=resolved_detail,
            **field_updates,
        )
    except OptimisticLockError:
        current = OperationRepository(session).get(operation_row.id)
        assert current is not None  # the row cannot have been deleted between read and write
        # Raises InvalidStateTransitionError if `current.state` no longer permits this
        # transition — the expected outcome for a genuine decision race. If it is
        # somehow still legal (no v1 transition changes `state_version` without also
        # changing `.state`, so this is defensive rather than reachable today), retry
        # once against the row's current version.
        state_machine.validate_transition(transition_id, from_state=current.state)
        updated, _event = OperationRepository(session).apply_transition(
            operation_id=current.id,
            expected_version=current.state_version,
            new_state=transition.to_state,
            transition=transition_id,
            from_state=current.state,
            actor=actor,
            detail=resolved_detail,
            **field_updates,
        )
    audit_writer.write(
        AuditLogRepository(session),
        actor=actor,
        action=_TRANSITION_ACTION[transition_id],
        subject_type="operation",
        subject_id=operation_row.id,
        outcome=_TRANSITION_OUTCOME[transition_id],
        detail=resolved_detail,
    )
    return updated


def _record_creation(
    session: Session,
    operation_row: OperationRow,
    *,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """T01: the one transition with no prior row to compare-and-set against — the row
    already exists at ``state="PREPARING"`` (``OperationRepository.create``); this only
    records the event and audit rows for that creation."""
    resolved_detail = detail or {}
    OperationEventRepository(session).append(
        operation_id=operation_row.id,
        from_state=None,
        to_state="PREPARING",
        transition="T01",
        actor=actor,
        detail=resolved_detail,
    )
    audit_writer.write(
        AuditLogRepository(session),
        actor=actor,
        action=_TRANSITION_ACTION["T01"],
        subject_type="operation",
        subject_id=operation_row.id,
        outcome=_TRANSITION_OUTCOME["T01"],
        detail=resolved_detail,
    )


def _apply_lazy_expiry(session: Session, operation_row: OperationRow) -> OperationRow:
    """T08/T11 if ``operation_row``'s deadline has already passed, else the row
    unchanged (invariant I9, ADR-010). ``actor="clock"`` — one of the three documented
    ``operation_events.actor`` values (principal ID, ``system``, or ``clock``,
    BUILD_PLAN section 8.1) — names this as a deadline firing, not a human or the
    system's own orchestration.

    Race-safe against a concurrent caller expiring the same row (phase 6's best-effort
    sweeper racing a request's own lazy expiry, or two overlapping sweeps): when
    ``_apply_and_audit`` finds someone else already moved this row between our read and
    our write, it re-validates against the row's current state and raises
    :class:`~n8n_operator.errors.InvalidStateTransitionError` if T08/T11 no longer
    applies there — exactly the "someone already resolved this" case, so the caller's
    precondition ("this row's overdue-ness has been resolved") still holds. A fresh
    read returns the now-settled row rather than propagating that error, and no second
    event or audit row is ever written for a transition that already landed once.
    """
    now = datetime.now(UTC)
    transition = state_machine.overdue_expiry_transition(
        state=operation_row.state,
        now=now,
        approval_expires_at=operation_row.approval_expires_at,
        execution_deadline=operation_row.execution_deadline,
    )
    if transition is None:
        return operation_row
    try:
        return _apply_and_audit(session, operation_row, transition.id, actor="clock")
    except InvalidStateTransitionError:
        refreshed = OperationRepository(session).get(operation_row.id)
        assert refreshed is not None  # the row cannot have been deleted between read and write
        return refreshed


def _get_operation_row(session: Session, operation_id: str) -> OperationRow:
    row = OperationRepository(session).get(operation_id)
    if row is None:
        raise OperationNotFoundError()
    return _apply_lazy_expiry(session, row)


def _get_owned_operation_row(
    session: Session, operation_id: str, principal_id: str
) -> OperationRow:
    """As :func:`_get_operation_row`, but also enforces that ``principal_id`` is the
    operation's own principal. A mismatch raises the identical
    :class:`~n8n_operator.errors.OperationNotFoundError` a nonexistent ID would — the
    same "no signal distinguishing X from Y" defense AC-01 states for the registry
    (a caller probing another principal's operation IDs learns nothing)."""
    row = _get_operation_row(session, operation_id)
    if row.principal_id != principal_id:
        raise OperationNotFoundError()
    return row


def _find_entry(document: RegistryDocument, workflow_id: str) -> WorkflowEntry | None:
    for entry in document.workflows:
        if entry.id == workflow_id:
            return entry
    return None


def _require_active_snapshot(session: Session) -> RegistrySnapshot:
    snapshot = RegistrySnapshotRepository(session).get_latest()
    if snapshot is None:
        raise RegistryUnavailableError()
    return snapshot


def _require_active_document(session: Session) -> RegistryDocument:
    return RegistryDocument.model_validate(_require_active_snapshot(session).document)


def _require_enabled_entry(document: RegistryDocument, workflow_id: str) -> WorkflowEntry:
    """Not found and disabled are deliberately different errors here (unlike discovery,
    which treats both as invisible) — MCP_TOOLS.md section 2.6 lists ``WORKFLOW_DISABLED``
    as a ``prepare_operation``-specific error distinct from ``WORKFLOW_NOT_FOUND``."""
    entry = _find_entry(document, workflow_id)
    if entry is None:
        raise WorkflowNotFoundError()
    if not entry.enabled:
        raise WorkflowDisabledError()
    return entry


def _entry_for_operation(session: Session, snapshot_id: str, workflow_id: str) -> WorkflowEntry:
    """The resolved entry an already-created operation's own (frozen) snapshot recorded
    for it — used to recover a workflow's limits/output policy at approve/execute time,
    as opposed to :func:`_require_active_document`, which is deliberately the *current*
    active snapshot (used only for the execute-time drift comparison)."""
    snapshot = RegistrySnapshotRepository(session).get(snapshot_id)
    assert (
        snapshot is not None
    )  # an operation's snapshot_id always names a real, immutable snapshot
    document = RegistryDocument.model_validate(snapshot.document)
    entry = _find_entry(document, workflow_id)
    assert (
        entry is not None
    )  # present and enabled when this operation was created; snapshots never change
    return entry


def _resolved_ttl(seconds: int | None) -> int:
    """Every ``WorkflowEntry`` reachable from a loaded snapshot document is already
    resolved (:func:`~n8n_operator.registry.schema.resolve_workflow_entry` ran at load
    time) — ``limits.approval_ttl_seconds``/``execution_ttl_seconds`` are typed
    ``int | None`` only to describe an *unresolved* entry, which never appears here."""
    assert seconds is not None
    return seconds


def _to_domain(row: OperationRow) -> Operation:
    return Operation(
        id=row.id,
        principal_id=row.principal_id,
        environment=row.environment,
        snapshot_id=row.snapshot_id,
        workflow_id=row.workflow_id,
        definition_hash=row.definition_hash,
        state=row.state,
        state_version=row.state_version,
        arguments=row.arguments,
        argument_fingerprint=row.argument_fingerprint,
        argument_bytes=row.argument_bytes,
        idempotency_key=row.idempotency_key,
        handle_burned_at=row.handle_burned_at,
        approval_expires_at=row.approval_expires_at,
        execution_deadline=row.execution_deadline,
        n8n_execution_id=row.n8n_execution_id,
        parent_operation_id=row.parent_operation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _execution_result_to_domain(row: ExecutionResultRow) -> ExecutionResult:
    return ExecutionResult(
        operation_id=row.operation_id,
        n8n_execution_id=row.n8n_execution_id,
        status=row.status,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        redacted_payload=row.redacted_payload,
        node_trace=row.node_trace,
        error=row.error,
    )


# ----------------------------------------------------------------------------------
# Registry use cases (Phase 2), and discovery use cases that build on them (Phase 3).
# None of these touch n8n.
# ----------------------------------------------------------------------------------


def validate_registry(path: Path, *, server_max_argument_bytes: int) -> LoadedRegistry:
    """Load and validate the registry at ``path`` without persisting anything."""
    return load_registry(path, server_max_argument_bytes=server_max_argument_bytes)


def get_active_snapshot(session: Session) -> RegistrySnapshot | None:
    """The currently active registry snapshot, or ``None`` before the first reload."""
    return RegistrySnapshotRepository(session).get_latest()


def reload_registry(
    session: Session,
    path: Path,
    *,
    server_max_argument_bytes: int,
    actor: str = "local",
) -> tuple[RegistrySnapshot, bool]:
    """Load, validate, and persist a new registry snapshot as the active one.

    Returns ``(snapshot, reused_existing)``: ``reused_existing`` is ``True`` when the
    loaded content hashes identically to an already-persisted snapshot, in which case
    that existing row is returned unchanged rather than creating a duplicate.

    **Validate before touching storage.** :func:`~n8n_operator.registry.loader.load_registry`
    raises before this function does anything to the database, so a registry that fails
    to load leaves the previously-active snapshot untouched.

    **Persist atomically.** The new snapshot, every one of its ``WorkflowBinding`` rows,
    and its audit entry are written inside the caller's transaction (this function does
    not commit).

    **Never affect already-prepared operations.** Snapshots and bindings are only ever
    inserted, never updated or deleted — any operation that recorded an older
    ``snapshot_id`` keeps referring to valid, unchanged data indefinitely.

    Only workflow entries with ``enabled: true`` get a ``WorkflowBinding`` row.
    """
    loaded = load_registry(path, server_max_argument_bytes=server_max_argument_bytes)

    snapshot_repo = RegistrySnapshotRepository(session)
    previous = snapshot_repo.get_latest()

    existing = snapshot_repo.get_by_content_hash(loaded.content_hash)
    if existing is not None:
        audit_writer.write(
            AuditLogRepository(session),
            actor=actor,
            action="registry.reloaded",
            subject_type="registry_snapshot",
            subject_id=existing.id,
            outcome="allowed",
            detail={
                "content_hash": loaded.content_hash,
                "reused_existing": True,
                "previous_snapshot_id": previous.id if previous else None,
            },
        )
        return existing, True

    snapshot = snapshot_repo.create(
        content_hash=loaded.content_hash,
        source_path=loaded.source_path,
        document=loaded.document,
    )

    binding_repo = WorkflowBindingRepository(session)
    for entry in loaded.entries:
        if not entry.enabled:
            continue
        assert entry.approval is not None  # loaded.entries are always resolved
        binding_repo.create(
            snapshot_id=snapshot.id,
            workflow_id=entry.id,
            n8n_workflow_id=entry.n8n_workflow_id,
            definition_hash=entry.definition_hash,
            side_effects=entry.side_effects,
            approval_policy=entry.approval,
            input_schema=entry.input_schema,
        )

    audit_writer.write(
        AuditLogRepository(session),
        actor=actor,
        action="registry.reloaded",
        subject_type="registry_snapshot",
        subject_id=snapshot.id,
        outcome="allowed",
        detail={
            "content_hash": loaded.content_hash,
            "reused_existing": False,
            "previous_snapshot_id": previous.id if previous else None,
        },
    )

    return snapshot, False


def list_workflows(
    session: Session,
    *,
    tags: Sequence[str] | None = None,
    risk: str | None = None,
    side_effects: str | None = None,
) -> list[WorkflowSummary]:
    """Every enabled workflow in the active snapshot (MCP_TOOLS.md section 2.1).

    ``tags`` matches a workflow that carries **all** listed tags; ``risk`` and
    ``side_effects`` match exactly. All three are optional and compose by intersection.
    Filtering, not authorization: an unmatched filter narrows what is *listed*, the same
    way it would for a caller who scrolled through the unfiltered list by hand — it is
    not a second gate alongside ``enabled`` (ADR-002 default-deny already is that gate).
    """
    document = _require_active_document(session)
    summaries = [WorkflowSummary.from_entry(entry) for entry in document.workflows if entry.enabled]
    if tags:
        wanted = set(tags)
        summaries = [s for s in summaries if wanted <= set(s.tags)]
    if risk is not None:
        summaries = [s for s in summaries if s.risk == risk]
    if side_effects is not None:
        summaries = [s for s in summaries if s.side_effects == side_effects]
    return summaries


def describe_workflow(session: Session, *, workflow_id: str) -> WorkflowDetail:
    """One workflow's full contract (MCP_TOOLS.md section 2.2). Disabled and absent are
    the same ``WORKFLOW_NOT_FOUND`` here — unlike ``prepare_operation``, discovery does
    not distinguish them (WORKFLOW_REGISTRY.md section 9.3: disabled "disappears from
    discovery")."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    return WorkflowDetail.from_entry(entry)


def validate_input(
    session: Session, *, workflow_id: str, arguments: dict[str, Any]
) -> list[ArgumentError]:
    """Check ``arguments`` against a workflow's schema without creating an operation
    (MCP_TOOLS.md section 2.4)."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    return validate_arguments(entry.input_schema, arguments)


def preflight_workflow(
    session: Session, *, workflow_id: str, preflight: PreflightPort
) -> PreflightResult:
    """Run the same checks ``prepare_operation`` runs, without creating an operation
    (MCP_TOOLS.md section 2.5)."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    return preflight.check(entry)


def get_instance_health(health: HealthPort) -> HealthCheckResult:
    """Whether the configured n8n instance is reachable (MCP_TOOLS.md section 2.3).

    Unlike every other use case here, this touches neither the registry nor storage —
    reachability is a property of the n8n instance alone, not of any one workflow or
    operation — so it takes no ``Session``. A thin pass-through over ``health.check()``,
    kept as a named use case (rather than the MCP adapter calling the port directly) so
    the "MCP calls core.service" rule (ADR-001) has no exception.
    """
    return health.check()


# ----------------------------------------------------------------------------------
# The operation lifecycle (BUILD_PLAN sections 5, 8.1; ARCHITECTURE.md section 4).
# ----------------------------------------------------------------------------------


def prepare_operation(
    session: Session,
    *,
    principal_id: str,
    environment: str,
    workflow_id: str,
    arguments: dict[str, Any],
    preflight: PreflightPort,
    server_max_argument_bytes: int,
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> tuple[Operation, bool, str | None]:
    """Validate, preflight, and mint an operation handle (ADR-003, ARCHITECTURE.md 4.1).

    Returns ``(operation, idempotent_replay, approval_token)`` — extends
    :func:`reload_registry`'s ``(result, reused)`` shape with the one-time raw approval
    token, when a new one was minted (``entry.approval == "required"`` and this was not
    an idempotent replay). Only ``storage.repository.ApprovalRepository`` ever persists
    the token's *hash* (``core/handles.py``); this is the only point in the codebase
    where the raw value is available at all, so a caller that wants to build an
    approval URL (the MCP adapter, for a local caller only — invariant I12, boundary
    B13) must capture it *here*, from this return value, or never again. An idempotent
    replay never re-mints, so it always returns ``None`` here even if the original
    ``prepare`` call minted one — reusing or reconstructing another call's token would
    make it not single-use.

    ``INVALID`` and ``BLOCKED`` are *results*, not exceptions: the call succeeded and
    produced a governed, audited outcome (MCP_TOOLS.md section 2.6). Only a failure to
    interpret the request at all (``WORKFLOW_NOT_FOUND``, ``WORKFLOW_DISABLED``,
    ``IDEMPOTENCY_CONFLICT``) or a refusal to record it (``ARGUMENTS_TOO_LARGE``)
    raises — and in both cases the returned token is necessarily ``None``.
    """
    snapshot = _require_active_snapshot(session)
    document = RegistryDocument.model_validate(snapshot.document)
    entry = _require_enabled_entry(document, workflow_id)
    assert entry.approval is not None  # resolved entries always carry a concrete value

    canonical_bytes = canonicalize_arguments(arguments)
    fingerprint = fingerprint_arguments(canonical_bytes)
    effective_limit = entry.limits.max_argument_bytes or server_max_argument_bytes

    try:
        check_argument_size(canonical_bytes, effective_limit=effective_limit)
    except ArgumentsTooLargeError as exc:
        audit_writer.write(
            AuditLogRepository(session),
            actor=principal_id,
            action="operation.prepare_denied",
            subject_type="workflow",
            subject_id=workflow_id,
            outcome="denied",
            detail={
                "code": exc.code,
                "size": len(canonical_bytes),
                "limit": effective_limit,
            },
        )
        # ADR-011: "the attempt is still audited" even though no operation row is ever
        # written. The caller's own `session_scope` rolls back on any exception
        # propagating out of it (storage/session.py) — which would silently take this
        # audit entry down with it, since nothing else in this call wrote anything to
        # roll back *from*. Committing here, before raising, ends the current
        # transaction with only the audit entry in it; the caller's subsequent rollback
        # then has nothing left to undo.
        session.commit()
        raise

    op_repo = OperationRepository(session)
    if idempotency_key is not None:
        existing = op_repo.find_by_idempotency(
            principal_id=principal_id,
            environment=environment,
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
        )
        resolution = resolve_idempotency(
            existing_fingerprint=existing.argument_fingerprint if existing else None,
            new_fingerprint=fingerprint,
        )
        if resolution is IdempotencyResolution.REPLAY:
            assert existing is not None
            existing = _apply_lazy_expiry(session, existing)
            return _to_domain(existing), True, None

    handle = mint_operation_handle()
    operation = op_repo.create(
        id=handle,
        principal_id=principal_id,
        environment=environment,
        snapshot_id=snapshot.id,
        workflow_id=workflow_id,
        definition_hash=entry.definition_hash,
        state="PREPARING",
        arguments=redact(arguments, entry.output.redact),
        argument_fingerprint=fingerprint,
        argument_bytes=len(canonical_bytes),
        idempotency_key=idempotency_key,
    )
    _record_creation(
        session, operation, actor=principal_id, detail={"reason": reason} if reason else {}
    )

    errors = validate_arguments(entry.input_schema, arguments)
    if errors:
        operation = _apply_and_audit(
            session,
            operation,
            "T02",
            actor="system",
            detail={"errors": [e.to_dict() for e in errors]},
        )
        return _to_domain(operation), False, None

    preflight_result = preflight.check(entry)
    checks_detail = {"checks": [c.model_dump(mode="json") for c in preflight_result.checks]}
    if not preflight_result.ready:
        operation = _apply_and_audit(
            session, operation, "T03", actor="system", detail=checks_detail
        )
        return _to_domain(operation), False, None

    now = datetime.now(UTC)
    approval_token: str | None = None
    if entry.approval == "none":
        # R5 guarantees side_effects == "read_only" whenever approval == "none".
        execution_deadline = now + timedelta(
            seconds=_resolved_ttl(entry.limits.execution_ttl_seconds)
        )
        operation = _apply_and_audit(
            session,
            operation,
            "T05",
            actor="system",
            detail=checks_detail,
            execution_deadline=execution_deadline,
        )
    else:
        approval_expires_at = now + timedelta(
            seconds=_resolved_ttl(entry.limits.approval_ttl_seconds)
        )
        operation = _apply_and_audit(
            session,
            operation,
            "T04",
            actor="system",
            detail=checks_detail,
            approval_expires_at=approval_expires_at,
        )
        minted = mint_approval_token()
        binding_hash = compute_approval_binding(
            operation_id=operation.id,
            principal_id=principal_id,
            argument_fingerprint=fingerprint,
            snapshot_id=snapshot.id,
            definition_hash=entry.definition_hash,
        )
        ApprovalRepository(session).create(
            operation_id=operation.id,
            token_hash=minted.token_hash,
            binding_hash=binding_hash,
            expires_at=approval_expires_at,
        )
        approval_token = minted.token

    return _to_domain(operation), False, approval_token


def approve_operation(
    session: Session,
    *,
    operation_id: str,
    decided_by: str,
    client_fingerprint: str | None = None,
) -> Operation:
    """T06: a human approves (ADR-010; both the CLI and the approval-page channel call
    this one use case). Not scoped to a preparing principal — in v1's single-principal
    model the approver and preparer are always the same ``local`` identity; v2's RBAC
    would gate this differently, but that gate is not this function's job.

    ``client_fingerprint`` is coarse request provenance for the audit trail
    (BUILD_PLAN section 8.1) — set by the web approval channel, left ``None`` by the
    CLI, which has no request to fingerprint.
    """
    row = _get_operation_row(session, operation_id)
    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    execution_deadline = datetime.now(UTC) + timedelta(
        seconds=_resolved_ttl(entry.limits.execution_ttl_seconds)
    )
    updated = _apply_and_audit(
        session, row, "T06", actor=decided_by, execution_deadline=execution_deadline
    )
    approval = ApprovalRepository(session).get_by_operation_id(operation_id)
    if approval is not None:
        ApprovalRepository(session).record_decision(
            approval_id=approval.id,
            decision="approved",
            decided_by=decided_by,
            client_fingerprint=client_fingerprint,
        )
    return _to_domain(updated)


def reject_operation(
    session: Session,
    *,
    operation_id: str,
    decided_by: str,
    client_fingerprint: str | None = None,
) -> Operation:
    """T07: a human rejects (ADR-010). ``client_fingerprint`` as :func:`approve_operation`."""
    row = _get_operation_row(session, operation_id)
    updated = _apply_and_audit(session, row, "T07", actor=decided_by)
    approval = ApprovalRepository(session).get_by_operation_id(operation_id)
    if approval is not None:
        ApprovalRepository(session).record_decision(
            approval_id=approval.id,
            decision="rejected",
            decided_by=decided_by,
            client_fingerprint=client_fingerprint,
        )
    return _to_domain(updated)


def _approval_decision_context(
    session: Session, row: OperationRow, approval_row: ApprovalRow | None
) -> ApprovalDecisionContext:
    """Build the shared decision-surface shape from an already-fetched, already-lazily-
    expired operation row. Not exported — both public entry points below fetch and
    lazy-expire the row their own way (one by operation ID, one by token), then share
    this one assembly step, so the two can never render the workflow/drift/deadline
    fields differently."""
    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    current_document = _require_active_document(session)
    current_entry = _find_entry(current_document, row.workflow_id)
    current_hash = current_entry.definition_hash if current_entry is not None else None
    return ApprovalDecisionContext(
        operation_id=row.id,
        workflow_id=row.workflow_id,
        title=entry.title,
        description=entry.description,
        risk=entry.risk,
        side_effects=entry.side_effects,
        state=row.state,
        arguments=row.arguments,
        registered_definition_hash=row.definition_hash,
        current_definition_hash=current_hash,
        drifted=current_hash != row.definition_hash,
        created_at=row.created_at,
        approval_expires_at=row.approval_expires_at,
        execution_deadline=row.execution_deadline,
        approval_required=approval_row is not None,
        decided=approval_row is not None and approval_row.decision is not None,
        decision=approval_row.decision if approval_row is not None else None,  # type: ignore[arg-type]
        decided_at=approval_row.decided_at if approval_row is not None else None,
        decided_by=approval_row.decided_by if approval_row is not None else None,
    )


def get_approval_decision_context(
    session: Session, *, operation_id: str, principal_id: str
) -> ApprovalDecisionContext:
    """Everything needed to render or review an approval decision by operation ID
    (ADR-010) — the CLI's ``operations approve``/``reject`` (before confirming) and
    ``operations approval-status`` both call this."""
    row = _get_owned_operation_row(session, operation_id, principal_id)
    approval_row = ApprovalRepository(session).get_by_operation_id(operation_id)
    return _approval_decision_context(session, row, approval_row)


def resolve_approval_token(session: Session, *, token: str) -> ApprovalDecisionContext:
    """Verify a raw approval token and return the same decision-surface shape
    :func:`get_approval_decision_context` does — the web approval channel's own entry
    point, reached only from there (the CLI names an operation ID directly and never
    holds a token to verify).

    Raises :class:`~n8n_operator.errors.ApprovalTokenInvalidError` for a token whose
    hash matches no approval row, :class:`~n8n_operator.errors.ApprovalTokenAlreadyUsedError`
    for one a decision was already recorded against, and
    :class:`~n8n_operator.errors.ApprovalNotPendingError` if the operation — after lazy
    expiry is applied — is no longer ``PENDING_APPROVAL`` for any other reason (expired,
    canceled by the CLI in the meantime, or otherwise moved on). The raw token is never
    logged: only its hash is ever compared, and only against what is already at rest.
    """
    token_hash = hash_approval_token(token)
    approval_row = ApprovalRepository(session).get_by_token_hash(token_hash)
    if approval_row is None:
        raise ApprovalTokenInvalidError()
    if approval_row.decision is not None:
        raise ApprovalTokenAlreadyUsedError()

    row = _get_operation_row(session, approval_row.operation_id)
    if row.state != "PENDING_APPROVAL":
        raise ApprovalNotPendingError(details={"current_state": row.state})

    expected_binding = compute_approval_binding(
        operation_id=row.id,
        principal_id=row.principal_id,
        argument_fingerprint=row.argument_fingerprint,
        snapshot_id=row.snapshot_id,
        definition_hash=row.definition_hash,
    )
    # Structurally unreachable in v1 (none of the five bound fields is ever updated
    # after an operation row is created) — an explicit, verified check rather than a
    # silently-trusted assumption; see `compute_approval_binding`'s docstring.
    assert expected_binding == approval_row.binding_hash, (
        "approval token binding mismatch — operation identity changed after mint"
    )

    return _approval_decision_context(session, row, approval_row)


def cancel_operation(
    session: Session, *, operation_id: str, principal_id: str, reason: str | None = None
) -> Operation:
    """T09/T12: the originating caller withdraws before execution (MCP_TOOLS.md 2.9).

    ``reason`` is advisory only, exactly like ``prepare_operation``'s (ADR-007) — it is
    recorded on the transition's audit detail for a human reading the trail later, and
    never affects whether the cancellation is allowed.
    """
    row = _get_owned_operation_row(session, operation_id, principal_id)
    if row.state == "PENDING_APPROVAL":
        transition_id = "T09"
    elif row.state == "APPROVED":
        transition_id = "T12"
    else:
        raise InvalidStateTransitionError(details={"current_state": row.state})
    updated = _apply_and_audit(
        session,
        row,
        transition_id,
        actor=principal_id,
        detail={"reason": reason} if reason else {},
    )
    return _to_domain(updated)


def execute_operation(
    session: Session, *, operation_id: str, handle: str, principal_id: str
) -> Operation:
    """T10: burn the handle and move to ``EXECUTING`` (ADR-003, ARCHITECTURE.md 4.3
    steps 0-6). Dispatch to n8n and resolving to T13/T14/T15 is deliberately **not**
    this function's job — see :func:`record_execution_outcome`, the seam Phase 4's real
    n8n adapter calls after dispatch, without core ever importing ``n8n/``.

    ``handle`` is the same value as ``operation_id`` (ADR-003: the operation ID *is* the
    handle) — the MCP tool contract carries both fields regardless, so a caller passing
    two different values is a real error (``ARGUMENT_MISMATCH``) worth catching before
    anything else, rather than one this function silently ignores by only ever looking
    at ``operation_id``.

    The definition-hash re-check here compares against the registry's own *current*
    active snapshot — genuine drift, catchable without n8n, e.g. from a ``registry
    reload`` that picked up a re-hashed definition since this operation was approved.
    It is not the *live n8n* drift check ARCHITECTURE.md 4.3 step 4 also describes; that
    additionally requires the n8n adapter and is Phase 4's job, layered on top of this.
    """
    if handle != operation_id:
        raise ArgumentMismatchError(details={"operation_id": operation_id, "handle": handle})

    row = _get_owned_operation_row(session, operation_id, principal_id)

    if row.handle_burned_at is not None:
        raise HandleAlreadyUsedError()
    if row.state == "PENDING_APPROVAL":
        raise ApprovalRequiredError()
    if row.state == "EXPIRED":
        raise OperationExpiredError()
    if row.state == "CANCELED":
        raise OperationCanceledError()
    if row.state != "APPROVED":
        raise HandleInvalidError(details={"current_state": row.state})

    document = _require_active_document(session)
    current_entry = _find_entry(document, row.workflow_id)
    current_hash = current_entry.definition_hash if current_entry is not None else None
    if current_hash != row.definition_hash:
        raise DefinitionDriftError(
            details={"registered": row.definition_hash, "current": current_hash}
        )

    burned = OperationRepository(session).burn_handle(operation_id=operation_id)
    if not burned:
        raise HandleAlreadyUsedError()

    updated = _apply_and_audit(session, row, "T10", actor=principal_id)
    return _to_domain(updated)


def record_execution_outcome(
    session: Session,
    *,
    operation_id: str,
    outcome: Literal["success", "error", "indeterminate"],
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    n8n_execution_id: str | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    known_secrets: Sequence[str] = (),
) -> Operation:
    """T13/T14/T15: record what n8n reported for an already-``EXECUTING`` operation.

    The seam Phase 4's n8n adapter calls after dispatching — this function never talks
    to n8n itself, only records what the caller already determined. ``result`` and
    ``error`` are mutually exclusive; whichever is given is redacted per the workflow's
    ``output.redact``, scrubbed of any string in ``known_secrets`` (boundary B5/B6), and
    size-capped per ``output.max_bytes`` (``core/redaction.py``) before persistence — the
    same shaping ``get_execution_result`` later reads back.

    **Never inferred to be a non-event.** ``outcome="indeterminate"`` records exactly
    what ADR-009 requires: a timeout or ambiguous response is recorded as unknown, never
    silently reclassified as success or failure by this or any other code path.
    """
    row = _get_operation_row(session, operation_id)
    if row.state != "EXECUTING":
        raise InvalidStateTransitionError(details={"current_state": row.state})

    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    raw_payload = result if result is not None else (error or {})
    shaped = redact(raw_payload, entry.output.redact)
    shaped = scrub_secrets(shaped, known_secrets)
    capped, truncated = cap_output(shaped, max_bytes=entry.output.max_bytes)

    ExecutionResultRepository(session).create(
        operation_id=operation_id,
        status=outcome,
        n8n_execution_id=n8n_execution_id,
        started_at=started_at,
        finished_at=finished_at,
        redacted_payload=capped if result is not None else {},
        error=capped if error is not None else None,
    )

    transition_id = {"success": "T13", "error": "T14", "indeterminate": "T15"}[outcome]
    updated = _apply_and_audit(
        session,
        row,
        transition_id,
        actor="system",
        detail={"truncated": truncated},
        n8n_execution_id=n8n_execution_id,
    )
    return _to_domain(updated)


def get_operation(session: Session, *, operation_id: str, principal_id: str) -> Operation:
    """Current state of one operation, applying any overdue expiry first (invariant I9,
    MCP_TOOLS.md section 2.7)."""
    row = _get_owned_operation_row(session, operation_id, principal_id)
    return _to_domain(row)


def list_operations(
    session: Session,
    *,
    principal_id: str,
    environment: str | None = None,
    workflow_id: str | None = None,
    states: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> list[Operation]:
    """Filterable history (MCP_TOOLS.md section 2.10) — applies lazy expiry to every
    returned row, since a list is a read like any other (invariant I9).

    ``cursor`` is opaque to the caller (MCP_TOOLS.md 2.10: "Opaque pagination cursor")
    but is, concretely, the ``operation_id`` of the last row a previous page returned:
    operation IDs are ULIDs, so ``id`` order and ``created_at`` order agree, and
    "everything strictly older than this ID" is a stable page boundary without a
    separate offset concept. The MCP adapter mints the next page's cursor from the
    last operation in a full page and omits it once a page comes back short.
    """
    if not (1 <= limit <= 100):
        raise InvalidArgumentsError(details={"limit": limit})
    if states is not None:
        unknown = [s for s in states if s not in STATES]
        if unknown:
            raise InvalidArgumentsError(details={"unknown_states": unknown})
    rows = OperationRepository(session).list(
        principal_id=principal_id,
        environment=environment,
        workflow_id=workflow_id,
        states=states,
        since=since,
        limit=limit,
        before_id=cursor,
    )
    return [_to_domain(_apply_lazy_expiry(session, row)) for row in rows]


def get_execution_result(
    session: Session, *, operation_id: str, principal_id: str
) -> ExecutionResult:
    """The redacted, size-capped result of a completed operation (MCP_TOOLS.md 2.11)."""
    _get_owned_operation_row(session, operation_id, principal_id)
    result_row = ExecutionResultRepository(session).get(operation_id)
    if result_row is None:
        raise ResultNotAvailableError()
    return _execution_result_to_domain(result_row)


def expire_overdue_operations(session: Session) -> int:
    """Apply every overdue T08/T11 transition, across every principal (ADR-010): the
    system-wide maintenance sweep ``n8n-operator operations expire`` and the approval
    app's best-effort sweeper both call. Not a substitute for lazy expiry — every read
    and action already applies T08/T11 to the one row it touches (invariant I9) — this
    is purely an audit-timeline-fidelity improvement, so an ``EXPIRED`` event lands near
    the deadline instead of at whatever moment something next reads the row.

    Returns the number of operations whose state actually changed. Concurrent callers
    (another sweep tick, a request's own lazy expiry landing on the same row first) are
    safe: :func:`_apply_lazy_expiry` treats a lost compare-and-set race as "already
    handled", not an error, so a row two callers reach at once is counted at most once
    in total, split however the race falls, never double-transitioned and never raised.
    """
    now = datetime.now(UTC)
    candidates = OperationRepository(session).list_overdue(now=now)
    changed = 0
    for row in candidates:
        before = row.state
        after = _apply_lazy_expiry(session, row)
        if after.state != before:
            changed += 1
    return changed
