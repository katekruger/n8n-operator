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

import base64
import hashlib
import json
import logging
import math
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.audit import writer as audit_writer
from n8n_operator.audit.chain import ChainVerificationResult, verify_chain
from n8n_operator.core import authorization, identity, state_machine
from n8n_operator.core.definition_diff import diff_canonical_definitions
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
    ApprovalDecisionEntry,
    ApprovalStatus,
    AuditEvent,
    AuditEventPage,
    DeliveryOutcome,
    DeliveryReceipt,
    DispatchOutcome,
    EnvironmentSummary,
    ExecutionLookup,
    ExecutionResult,
    HealthCheckResult,
    LatencyPercentiles,
    MetricsBreakdownEntry,
    MetricsResult,
    MetricsTotals,
    NotificationEvent,
    Operation,
    PreflightCheck,
    PreflightResult,
    ReconciliationRecord,
    RequestApprovalResult,
    WorkflowContract,
    WorkflowDefinitionDiff,
)
from n8n_operator.core.redaction import cap_output, redact, scrub_secrets
from n8n_operator.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalNotPendingError,
    ApprovalRequiredError,
    ApprovalTokenAlreadyUsedError,
    ApprovalTokenInvalidError,
    ApproverNotInPolicyError,
    ArgumentMismatchError,
    ArgumentsTooLargeError,
    ConcurrencyLimitReachedError,
    DefinitionDriftError,
    EnvironmentArchivedError,
    HandleAlreadyUsedError,
    HandleInvalidError,
    InstanceUnreachableError,
    InsufficientRoleError,
    InvalidArgumentsError,
    InvalidStateTransitionError,
    OperationCanceledError,
    OperationExpiredError,
    OperationNotFoundError,
    OperatorError,
    OptimisticLockError,
    RateLimitedError,
    ReconciliationNotApplicableError,
    RegistryUnavailableError,
    ResultNotAvailableError,
    RetryNotApplicableError,
    WorkflowDisabledError,
    WorkflowInactiveError,
    WorkflowMissingOnInstanceError,
    WorkflowNotFoundError,
)
from n8n_operator.n8n.canonicalization import canonical_form, compute_definition_hash
from n8n_operator.registry.loader import LoadedOverlay, LoadedRegistry, load_overlay, load_registry
from n8n_operator.registry.schema import (
    RegistryDocument,
    WorkflowDetail,
    WorkflowEntry,
    WorkflowOverlayEntry,
    WorkflowSummary,
    resolve_overlay,
)
from n8n_operator.registry.validation import ArgumentError, validate_arguments
from n8n_operator.storage.models import STATES, RegistrySnapshot
from n8n_operator.storage.models import Approval as ApprovalRow
from n8n_operator.storage.models import ExecutionResult as ExecutionResultRow
from n8n_operator.storage.models import Operation as OperationRow
from n8n_operator.storage.repository import (
    ApprovalRepository,
    AuditLogRepository,
    EnvironmentRepository,
    ExecutionResultRepository,
    NotificationDeliveryRepository,
    OperationEventRepository,
    OperationRepository,
    OrganizationMembershipRepository,
    RegistrySnapshotRepository,
    WorkflowBindingRepository,
    WorkflowDefinitionSnapshotRepository,
    WorkflowEnvironmentOverlayRepository,
)
from n8n_operator.storage.session import session_scope

_logger = logging.getLogger(__name__)

__all__ = [
    "MAX_RETRY_CHAIN_DEPTH",
    "DispatchPort",
    "HealthPort",
    "NotificationSink",
    "PreflightPort",
    "ReconciliationPort",
    "WorkflowDefinitionPort",
    "approve_operation",
    "cancel_operation",
    "check_and_deliver_alerts",
    "describe_workflow",
    "diff_workflow_definition",
    "dispatch_operation",
    "execute_operation",
    "expire_overdue_operations",
    "export_audit_record",
    "get_active_snapshot",
    "get_approval_decision_context",
    "get_approval_status",
    "get_execution_result",
    "get_instance_health",
    "get_metrics",
    "get_operation",
    "list_audit_events",
    "list_operations",
    "list_reconciliation_events",
    "list_workflows",
    "preflight_workflow",
    "prepare_operation",
    "reconcile_operation",
    "record_execution_outcome",
    "reject_operation",
    "reload_registry",
    "request_approval",
    "resolve_approval_token",
    "retry_failed_notifications",
    "retry_operation",
    "validate_input",
    "validate_registry",
    "verify_audit_chain",
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


class DispatchPort(Protocol):
    """What ``dispatch_operation`` needs from an n8n dispatcher.

    Same seam as :class:`PreflightPort`/:class:`HealthPort`, for one extra concern
    beyond the actual dispatch: ``fetch_node_trace`` is the one, deliberately narrow
    exception to "``mcp/`` never calls ``n8n/`` directly" that ``get_execution_log``
    needs — reached only after ``dispatch`` recorded a trustworthy execution ID
    (``core.models.DispatchOutcome.correlation_available``) *and* the workflow's own
    ``output.include_node_trace`` opts in. The composition root injects the real
    ``n8n.dispatch.N8nDispatch`` adapter; tests inject a fake.
    """

    def dispatch(
        self, workflow: WorkflowContract, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome: ...

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None: ...


class ReconciliationPort(Protocol):
    """What ``reconcile_operation`` needs from an n8n execution lookup (stage 06,
    ADR-009). Same seam as :class:`PreflightPort`/:class:`HealthPort`/
    :class:`DispatchPort` — the composition root (the ``operations reconcile record``
    CLI command, this stage's own composition root, mirroring
    ``cli/commands/health.py``'s ``_CliHealthAdapter``) injects a real
    ``n8n.client.N8nClient``-backed adapter; tests inject a fake. Deliberately the
    narrowest possible read: exact-ID lookup only, never a search or a listing — this
    port cannot itself become a heuristic-matching mechanism (ADR-009's own rejected
    alternative)."""

    def get_execution(self, execution_id: str) -> ExecutionLookup: ...


class WorkflowDefinitionPort(Protocol):
    """What ``diff_workflow_definition`` needs from a live n8n workflow-definition
    fetch (stage 07, ADR-008) — the same live read ``n8n/preflight.py``'s own drift
    check already makes. Unlike :class:`PreflightPort`/:class:`HealthPort`/
    :class:`DispatchPort`/:class:`ReconciliationPort`, this port needs no adapter
    class in the composition root at all: ``n8n.client.N8nClient.get_workflow``
    already returns a plain ``dict[str, Any]`` (never an ``n8n/``-local Pydantic
    type — see that method's own docstring), so a real client satisfies this
    Protocol structurally as-is."""

    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]: ...


class NotificationSink(Protocol):
    """What ``request_approval`` (and, stage 08, alert hooks) need from a delivery
    channel (ADR-018) — one interface for both event sources, the same "one seam,
    several implementations" shape :class:`PreflightPort`/:class:`HealthPort`/
    :class:`DispatchPort` already establish. The composition root injects a real
    local or webhook sink (``notifications/``); tests inject a fake. Dedup and
    bounded retry live in :func:`_deliver_with_dedup`, one layer above this port —
    an implementation's ``deliver`` is called at most once per delivery attempt, not
    responsible for deciding whether *to* attempt."""

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome: ...


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


def _get_operation_row_for_update(session: Session, operation_id: str) -> OperationRow:
    """As :func:`_get_operation_row`, but holds a row lock for the rest of the
    caller's transaction — used only by the approve/reject decision path, where two
    genuinely concurrent decisions on the same operation must serialize around
    reading-then-tallying (:meth:`OperationRepository.get_for_update`'s own
    docstring has the full reasoning)."""
    row = OperationRepository(session).get_for_update(operation_id)
    if row is None:
        raise OperationNotFoundError()
    return _apply_lazy_expiry(session, row)


def _authorize(
    session: Session,
    *,
    principal_id: str,
    tool_name: str,
    workflow_id: str | None,
    enable_v2: bool,
    environment_id: str | None = None,
    environment_organization_id: str | None = None,
    requester_principal_id: str | None = None,
    decider_principal_id: str | None = None,
) -> authorization.AuthorizationDecision:
    """The one authorization checkpoint every gated use case below calls (ADR-015,
    Stage 03). ``enable_v2=False`` (v1, and v2's default before an operator opts in) is
    always allowed without a database round trip — v1 behavior is byte-identical to
    before this stage, the completion gate's explicit requirement, because this
    function simply never runs its check in that mode. In v2 mode, fetches the
    caller's active memberships fresh, on every call — never cached, the same
    "disabled/removed is re-checked live" discipline Stage 02 already established for
    identity, extended here to role and scope (mid-session revocation, conflicting
    grants: yesterday's decision is never reused). Logs the outcome for operators
    (``reason_code`` — internal-only, see ``core/authorization.py``'s module
    docstring) but returns the decision rather than raising; each call site decides
    which existing not-found exception a denial becomes, so a denial is indistinguishable
    from absence by construction (invariant I14) rather than by a shared exception type
    that could itself become an oracle.
    """
    if not enable_v2:
        return authorization.AuthorizationDecision(allowed=True, reason_code="V1_UNGATED")
    memberships = OrganizationMembershipRepository(session).list_active_for_principal(principal_id)
    decision = authorization.evaluate(
        memberships=memberships,
        tool_name=tool_name,
        workflow_id=workflow_id,
        environment_id=environment_id,
        environment_organization_id=environment_organization_id,
        requester_principal_id=requester_principal_id,
        decider_principal_id=decider_principal_id,
    )
    _logger.info(
        "authorization_decision",
        extra={
            "principal_id": principal_id,
            "tool_name": tool_name,
            "workflow_id": workflow_id,
            "environment_id": environment_id,
            "allowed": decision.allowed,
            "reason_code": decision.reason_code,
        },
    )
    return decision


def _overlay_entry_from_row(row: Any, *, workflow_id: str) -> WorkflowOverlayEntry:
    return WorkflowOverlayEntry(
        workflow_id=workflow_id,
        n8n_workflow_id=row.n8n_workflow_id,
        definition_hash=row.definition_hash,
        trigger_path=row.trigger_path,
        trigger_secret_ref=row.trigger_secret_ref,
        approval_override=row.approval_override,
        limits_override=row.limits_override,
    )


def _apply_environment(
    session: Session,
    *,
    principal_id: str,
    environment: str | None,
    workflow_id: str,
    base_entry: WorkflowEntry,
    tool_name: str,
    enable_v2: bool,
    forbid_archived: bool = False,
) -> tuple[str | None, WorkflowEntry]:
    """Resolve the environment (Stage 04, ADR-016) and apply its overlay (if any) on
    top of ``base_entry`` (already resolved from the active registry snapshot by the
    caller) — the one place environment resolution, authorization, and overlay merge
    happen together, so the three can never drift out of step for a given call. Also
    performs the workflow-scope/role-capability authorization check
    (:func:`_authorize`, Stage 03), now finally reachable with a real
    ``environment_id`` instead of ``None`` (closes THREAT_MODEL.md RR-13).

    ``enable_v2=False`` (v1, unchanged): returns ``(None, base_entry)`` — no
    environment concept reaches v1 callers at all, literally the same result every
    pre-stage-04 call already got.

    Raises :class:`WorkflowNotFoundError` on an authorization denial (the identical
    shape a nonexistent workflow already produces — invariant I14), and
    :class:`EnvironmentArchivedError` when ``forbid_archived`` and the resolved
    environment is archived (state-changing calls only — AC-47; read tools pass
    ``forbid_archived=False``, the default, since an archived environment must stay
    resolvable for historical operations).
    """
    if not enable_v2:
        return None, base_entry

    resolved_env = identity.resolve_environment(
        session, principal_id=principal_id, environment=environment
    )
    if forbid_archived and resolved_env.archived_at is not None:
        raise EnvironmentArchivedError()

    decision = _authorize(
        session,
        principal_id=principal_id,
        tool_name=tool_name,
        workflow_id=workflow_id,
        environment_id=resolved_env.id,
        environment_organization_id=resolved_env.organization_id,
        enable_v2=enable_v2,
    )
    if not decision.allowed:
        raise WorkflowNotFoundError()

    overlay_row = WorkflowEnvironmentOverlayRepository(session).get(workflow_id, resolved_env.id)
    overlay_entry = (
        _overlay_entry_from_row(overlay_row, workflow_id=workflow_id)
        if overlay_row is not None
        else None
    )
    merged_entry = resolve_overlay(base_entry, overlay_entry)
    return resolved_env.id, merged_entry


def _compute_eligible_approvers(
    session: Session,
    *,
    organization_id: str,
    workflow_id: str,
    environment_id: str,
    requester_principal_id: str,
) -> list[str]:
    """The approval-policy snapshot's eligible-approver list (stage 05, ADR-017
    section 1): every distinct principal, active in this organization, whose roles
    include ``approver`` or ``admin`` (both carry
    ``authorization.APPROVE_REJECT_CAPABILITY`` — ``ROLE_CAPABILITIES``) *and* whose
    grant's ``workflow_scope``/``environment_scope`` covers this workflow and
    environment (the identical per-membership conjunction ``authorization.evaluate``
    already applies, reused here rather than re-derived) — **excluding the
    requester, structurally**, regardless of whether they hold ``approver``
    elsewhere. A principal holding two qualifying memberships counts once (a
    `principal_id` set, not a membership-row count) — the same identity can never
    occupy two quorum slots.

    Returns a sorted list for a deterministic, reproducible snapshot — the exact
    list this function returns *is* the policy from this moment forward, frozen onto
    the operation row by the caller (T04) and never recomputed.
    """
    memberships = OrganizationMembershipRepository(session).list_active_for_organization(
        organization_id
    )
    eligible: set[str] = set()
    for membership in memberships:
        if membership.principal_id == requester_principal_id:
            continue
        capable_roles = [
            role
            for role in membership.roles
            if authorization.APPROVE_REJECT_CAPABILITY in authorization.capabilities_for_role(role)
        ]
        if not capable_roles:
            continue
        if not authorization.match_workflow_scope(membership.workflow_scope, workflow_id):
            continue
        if not authorization.environment_scope_covers(membership, environment_id, organization_id):
            continue
        eligible.add(membership.principal_id)
    return sorted(eligible)


def _get_owned_operation_row(
    session: Session,
    operation_id: str,
    principal_id: str,
    *,
    tool_name: str = "get_operation",
    enable_v2: bool = False,
) -> OperationRow:
    """As :func:`_get_operation_row`, but also enforces that the caller may see this
    operation: either it is their own (v1's rule, unchanged), or — only in v2 mode —
    the evaluator authorizes ``tool_name`` for the operation's own ``workflow_id``
    (ADR-015's role-based visibility: an approver/admin legitimately reaches operations
    they did not personally create). A denial, either way, raises the identical
    :class:`~n8n_operator.errors.OperationNotFoundError` a nonexistent ID would — the
    same "no signal distinguishing X from Y" defense AC-01 states for the registry
    (a caller probing another principal's operation IDs learns nothing), extended
    across the organization boundary (invariant I14)."""
    row = _get_operation_row(session, operation_id)
    if row.principal_id == principal_id:
        return row
    if not enable_v2:
        # v1's rule, unchanged: ownership is the only check, and it already failed
        # above. `_authorize` must not be consulted here — it always allows when
        # `enable_v2=False` (v1 stays byte-identical to before this stage for every
        # *gated* check), which would silently defeat the ownership check itself.
        raise OperationNotFoundError()
    environment_organization_id: str | None = None
    if row.environment_id is not None:
        environment_row = EnvironmentRepository(session).get(row.environment_id)
        # A real, prior operation's own `environment_id` always still names a real row
        # — environments are archived, never deleted (ADR-016 section 4).
        assert environment_row is not None
        environment_organization_id = environment_row.organization_id
    decision = _authorize(
        session,
        principal_id=principal_id,
        tool_name=tool_name,
        workflow_id=row.workflow_id,
        environment_id=row.environment_id,
        environment_organization_id=environment_organization_id,
        enable_v2=enable_v2,
    )
    if not decision.allowed:
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


def reload_overlay(
    session: Session, path: Path, *, environment_id: str, actor: str = "local"
) -> LoadedOverlay:
    """Load, validate (rules R13-R14, ADR-016), and persist one environment's overlay
    file — validated against the **current active base registry snapshot**, not
    whatever the base meant when the overlay was originally authored.

    Unlike :func:`reload_registry`, this writes no immutable snapshot of its own:
    ``workflow_environment_overlays`` rows are deliberately mutable (see
    ``WorkflowEnvironmentOverlayRepository``'s own docstring) — a *reload* replaces the
    full set of overlays this environment has, the same way re-applying a config file
    would. Every overlay entry the file names is upserted; any existing overlay row for
    this environment whose ``workflow_id`` the file no longer names is deleted, so a
    workflow removed from the file is no longer overridden, not silently left with a
    stale prior override. What must never change retroactively is what an
    already-*prepared* operation was governed by — that guarantee comes from
    ``core.service`` resolving and freezing the merged contract once, at
    ``prepare_operation`` time, never from re-reading this table later.
    """
    document = _require_active_document(session)
    loaded = load_overlay(path, base_document=document, base_resolved_entries=document.workflows)

    overlay_repo = WorkflowEnvironmentOverlayRepository(session)
    named_workflow_ids = {overlay.workflow_id for overlay in loaded.overlays}
    for existing in overlay_repo.list_for_environment(environment_id):
        if existing.workflow_id not in named_workflow_ids:
            overlay_repo.delete(existing.workflow_id, environment_id)
    for overlay in loaded.overlays:
        overlay_repo.upsert(
            workflow_id=overlay.workflow_id,
            environment_id=environment_id,
            n8n_workflow_id=overlay.n8n_workflow_id,
            definition_hash=overlay.definition_hash,
            trigger_path=overlay.trigger_path,
            trigger_secret_ref=overlay.trigger_secret_ref,
            approval_override=overlay.approval_override,
            limits_override=overlay.limits_override,
        )

    audit_writer.write(
        AuditLogRepository(session),
        actor=actor,
        action="overlay.reloaded",
        subject_type="environment",
        subject_id=environment_id,
        outcome="allowed",
        detail={"source_path": loaded.source_path, "overlay_count": len(loaded.overlays)},
    )
    return loaded


def list_workflows(
    session: Session,
    *,
    tags: Sequence[str] | None = None,
    risk: str | None = None,
    side_effects: str | None = None,
    principal_id: str | None = None,
    enable_v2: bool = False,
    environment: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> list[WorkflowSummary]:
    """Every enabled workflow in the active snapshot (MCP_TOOLS.md section 2.1).

    ``tags`` matches a workflow that carries **all** listed tags; ``risk`` and
    ``side_effects`` match exactly. All three are optional and compose by intersection.
    Filtering, not authorization: an unmatched filter narrows what is *listed*, the same
    way it would for a caller who scrolled through the unfiltered list by hand — it is
    not a second gate alongside ``enabled`` (ADR-002 default-deny already is that gate).

    v2 (``enable_v2=True``): resolves one environment for the whole call (Stage 04,
    ``core.identity.resolve_environment`` — a caller wanting the resolved ID for its
    own result envelope calls that directly, the same function this uses), excludes
    workflows outside the caller's role/workflow-scope for that environment
    (MCP_TOOLS.md section 5.9), and reflects that environment's overlay — a workflow
    overlaid to ``approval: required`` in this environment shows that, not the base
    entry's own value.

    ``limit``/``cursor`` (v2 only — MCP_TOOLS.md section 5.9, "same shape as v1
    ``list_operations``"): applied *after* every filter above, including the
    authorization filter, so a cursor can never walk past an entry a filter would
    have hidden (the same pagination-side-channel discipline
    ``list_operations``/``OperationRepository.list`` already establish). Workflow IDs
    have no ULID-style sortable timestamp, so the cursor is simply "every entry whose
    ``id`` sorts strictly after this one", over the same deterministic, alphabetical
    ``id`` ordering every page uses — stable regardless of how the registry document
    itself orders its own ``workflows`` list. v1 (``enable_v2=False``) ignores both:
    the full, unpaginated list every v1 caller has always received.
    """
    document = _require_active_document(session)
    entries = [entry for entry in document.workflows if entry.enabled]

    if enable_v2:
        assert principal_id is not None  # every v2 caller is authenticated
        resolved_env = identity.resolve_environment(
            session, principal_id=principal_id, environment=environment
        )
        memberships = OrganizationMembershipRepository(session).list_active_for_principal(
            principal_id
        )
        merged_entries = []
        for entry in entries:
            if not authorization.evaluate(
                memberships=memberships,
                tool_name="list_workflows",
                workflow_id=entry.id,
                environment_id=resolved_env.id,
                environment_organization_id=resolved_env.organization_id,
            ).allowed:
                continue
            overlay_row = WorkflowEnvironmentOverlayRepository(session).get(
                entry.id, resolved_env.id
            )
            overlay_entry = (
                _overlay_entry_from_row(overlay_row, workflow_id=entry.id)
                if overlay_row is not None
                else None
            )
            merged_entries.append(resolve_overlay(entry, overlay_entry))
        entries = merged_entries

    summaries = [WorkflowSummary.from_entry(entry) for entry in entries]
    if tags:
        wanted = set(tags)
        summaries = [s for s in summaries if wanted <= set(s.tags)]
    if risk is not None:
        summaries = [s for s in summaries if s.risk == risk]
    if side_effects is not None:
        summaries = [s for s in summaries if s.side_effects == side_effects]

    if enable_v2:
        if not (1 <= limit <= 100):
            raise InvalidArgumentsError(details={"limit": limit})
        summaries = sorted(summaries, key=lambda s: s.workflow_id)
        if cursor is not None:
            summaries = [s for s in summaries if s.workflow_id > cursor]
        summaries = summaries[:limit]

    return summaries


def describe_workflow(
    session: Session,
    *,
    workflow_id: str,
    principal_id: str | None = None,
    enable_v2: bool = False,
    environment: str | None = None,
) -> WorkflowDetail:
    """One workflow's full contract (MCP_TOOLS.md section 2.2). Disabled and absent are
    the same ``WORKFLOW_NOT_FOUND`` here — unlike ``prepare_operation``, discovery does
    not distinguish them (WORKFLOW_REGISTRY.md section 9.3: disabled "disappears from
    discovery")."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    if principal_id is not None:
        _, entry = _apply_environment(
            session,
            principal_id=principal_id,
            environment=environment,
            workflow_id=workflow_id,
            base_entry=entry,
            tool_name="describe_workflow",
            enable_v2=enable_v2,
        )
    return WorkflowDetail.from_entry(entry)


def validate_input(
    session: Session,
    *,
    workflow_id: str,
    arguments: dict[str, Any],
    principal_id: str | None = None,
    enable_v2: bool = False,
    environment: str | None = None,
) -> list[ArgumentError]:
    """Check ``arguments`` against a workflow's schema without creating an operation
    (MCP_TOOLS.md section 2.4)."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    if principal_id is not None:
        _, entry = _apply_environment(
            session,
            principal_id=principal_id,
            environment=environment,
            workflow_id=workflow_id,
            base_entry=entry,
            tool_name="validate_input",
            enable_v2=enable_v2,
        )
    return validate_arguments(entry.input_schema, arguments)


def preflight_workflow(
    session: Session,
    *,
    workflow_id: str,
    preflight: PreflightPort,
    principal_id: str | None = None,
    enable_v2: bool = False,
    environment: str | None = None,
) -> PreflightResult:
    """Run the same checks ``prepare_operation`` runs, without creating an operation
    (MCP_TOOLS.md section 2.5)."""
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    if principal_id is not None:
        _, entry = _apply_environment(
            session,
            principal_id=principal_id,
            environment=environment,
            workflow_id=workflow_id,
            base_entry=entry,
            tool_name="preflight_workflow",
            enable_v2=enable_v2,
        )
    result = preflight.check(entry)
    return result.model_copy(
        update={
            "checks": [_with_diff_hint(check, workflow_id=workflow_id) for check in result.checks]
        }
    )


def _with_diff_hint(check: PreflightCheck, *, workflow_id: str) -> PreflightCheck:
    """Points a failed ``definition_unchanged`` check at ``diff_workflow_definition``/
    ``registry diff-live`` (stage 07) — advisory only, never consulted by any gating
    code path (ADR-008's "advisory, not deciding"; the hash comparison this check
    already ran, not the linked diff, is what actually failed this check)."""
    if check.check != "definition_unchanged" or check.status != "fail":
        return check
    detail = dict(check.detail) if isinstance(check.detail, dict) else {}
    detail["diff_hint"] = (
        f"Run `n8n-operator registry diff-live {workflow_id}` to see what changed."
    )
    return check.model_copy(update={"detail": detail})


def _redact_credential_ids(canonical: dict[str, Any], *, salt: str) -> dict[str, Any]:
    """Every ``nodes[].credentials.*.id`` value replaced by a salted digest — applied
    identically to both sides of a diff *before* diffing, so an unchanged credential
    binding still produces no diff entry (equal ids → equal digests) and a changed one
    still shows as ``modified`` (different ids → different digests), but the raw
    credential id itself is never in the result either way. Credential-binding
    *presence* is semantic (CAN-05) and must stay visible; the identifier itself never
    needs to be, and this makes forgetting to redact it structurally impossible rather
    than a redaction pass that could be skipped at one call site."""

    def _digest(raw_id: Any) -> str:
        return f"[REDACTED:{hashlib.sha256(f'{salt}{raw_id}'.encode()).hexdigest()[:12]}]"

    nodes = []
    for node in canonical.get("nodes", []):
        credentials = node.get("credentials")
        if isinstance(credentials, dict):
            node = {
                **node,
                "credentials": {
                    cred_type: (
                        {**cred_ref, "id": _digest(cred_ref["id"])}
                        if isinstance(cred_ref, dict) and "id" in cred_ref
                        else cred_ref
                    )
                    for cred_type, cred_ref in credentials.items()
                },
            }
        nodes.append(node)
    return {**canonical, "nodes": nodes}


def _redact_diff_value(value: Any, path: str, redact_paths: Sequence[str]) -> Any:
    """Apply ``output.redact`` (JSONPath, authored against a tool *result* shape) to a
    single diff entry's ``registered_value``/``live_value``. A diff entry's value is
    usually a bare scalar (a string, a number) — :func:`~n8n_operator.core.redaction.
    redact` can only mutate a matched *child* of a dict/list it is given, never a bare
    root value, so the value is wrapped under a synthetic key named for the field the
    diff path itself ends in (``/settings/secretField`` -> field ``secretField``) before
    redaction, then unwrapped. This lets an author write the same
    ``$.secretField``-style pattern they would for that field anywhere else."""
    if not redact_paths or value is None:
        return value
    field = path.rsplit("/", 1)[-1]
    wrapped = redact({field: value}, redact_paths)
    return wrapped.get(field, value)


def diff_workflow_definition(
    session: Session,
    *,
    workflow_id: str,
    definition: WorkflowDefinitionPort,
    principal_id: str | None = None,
    enable_v2: bool = False,
    environment: str | None = None,
    known_secrets: Sequence[str] = (),
) -> WorkflowDefinitionDiff:
    """Structural diff between the registered ``definition_hash`` and the live n8n
    definition (stage 07, MCP_TOOLS.md section 5.6, ADR-008) — advisory only, presented
    to a human *after* the hash comparison (the sole gate, unchanged) has already
    decided pass/fail. Resolution mirrors :func:`describe_workflow`/
    :func:`preflight_workflow` exactly, so AC-44's bitwise-identical
    unauthorized-vs-nonexistent requirement holds by reusing the same mechanism those
    tools already prove it for.
    """
    document = _require_active_document(session)
    entry = _find_entry(document, workflow_id)
    if entry is None or not entry.enabled:
        raise WorkflowNotFoundError()
    resolved_environment_id: str | None = None
    if principal_id is not None:
        resolved_environment_id, entry = _apply_environment(
            session,
            principal_id=principal_id,
            environment=environment,
            workflow_id=workflow_id,
            base_entry=entry,
            tool_name="diff_workflow_definition",
            enable_v2=enable_v2,
        )

    live_raw = definition.get_workflow(entry.n8n_workflow_id)
    live_hash = compute_definition_hash(live_raw)
    changed = live_hash != entry.definition_hash

    snapshot = WorkflowDefinitionSnapshotRepository(session).get(
        workflow_id=workflow_id, definition_hash=entry.definition_hash
    )
    if snapshot is None:
        return WorkflowDefinitionDiff(
            workflow_id=workflow_id,
            environment=resolved_environment_id if enable_v2 else None,
            registered_hash=entry.definition_hash,
            live_hash=live_hash,
            changed=changed,
            diff=[],
            diff_available=False,
            note=(
                "no stored snapshot for this hash — run `registry hash "
                "--workflow-id ... --n8n-workflow-id ...` to capture one"
            ),
        )

    # Credential-id digesting (structural, always-on) happens before diffing — it
    # must, so an unchanged binding never shows as a false `modified` and a changed
    # one always does (equal ids -> equal digests, different ids -> different
    # digests), without ever exposing the raw id either way.
    salt = secrets.token_hex(16)
    registered_canonical = _redact_credential_ids(snapshot.canonical_definition, salt=salt)
    live_canonical = _redact_credential_ids(canonical_form(live_raw), salt=salt)
    diff_entries, truncated, total_changes = diff_canonical_definitions(
        registered_canonical, live_canonical
    )

    # `output.redact`/`scrub_secrets` are applied to each entry's own *value* only,
    # after diffing — never before: redacting first would make two genuinely
    # different values compare equal (both "[REDACTED]"), silently erasing the
    # entry entirely rather than masking its content. MCP_TOOLS.md section 5.6's own
    # example shows exactly this shape — a `modified` entry whose `registered_value`/
    # `live_value` are both `"[REDACTED]"`, still present as an entry, never absent.
    redact_paths = entry.output.redact
    redacted_entries = []
    for diff_entry in diff_entries:
        registered_value = _redact_diff_value(
            diff_entry.registered_value, diff_entry.path, redact_paths
        )
        live_value = _redact_diff_value(diff_entry.live_value, diff_entry.path, redact_paths)
        if known_secrets:
            registered_value = scrub_secrets(registered_value, known_secrets)
            live_value = scrub_secrets(live_value, known_secrets)
        redacted_entries.append(
            diff_entry.model_copy(
                update={"registered_value": registered_value, "live_value": live_value}
            )
        )

    return WorkflowDefinitionDiff(
        workflow_id=workflow_id,
        environment=resolved_environment_id if enable_v2 else None,
        registered_hash=entry.definition_hash,
        live_hash=live_hash,
        changed=changed,
        diff=redacted_entries,
        diff_available=True,
        truncated=truncated,
        total_changes=total_changes,
    )


_METRICS_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
}


def _resolve_scope(
    session: Session, *, principal_id: str, environment: str | None, tool_name: str, enable_v2: bool
) -> tuple[str | None, list[str] | None]:
    """``(resolved_environment, workflow_id_like_patterns)`` for a tool with no single
    ``workflow_id`` of its own — ``get_metrics``/``list_audit_events`` (stage 08).
    Mirrors ``list_operations``'s own scope-resolution exactly (not
    ``_apply_environment``, which is workflow-specific): resolves a real environment
    the same way every v2 tool does, then gathers ``LIKE`` patterns from every active
    membership whose ``environment_scope == ["*"]`` and whose roles grant this tool's
    own capability — an environment-scoped-only membership contributes nothing here,
    the same simplification ``list_operations`` already makes. ``v1``
    (``enable_v2=False``): no scope concept exists at all, returns ``(environment,
    None)`` — ``None`` patterns means "no restriction" to every caller below."""
    if not enable_v2:
        return environment, None
    resolved_environment = identity.resolve_environment(
        session, principal_id=principal_id, environment=environment
    ).id
    memberships = OrganizationMembershipRepository(session).list_active_for_principal(principal_id)
    patterns: list[str] = []
    for membership in memberships:
        if tool_name not in {
            tool for role in membership.roles for tool in authorization.capabilities_for_role(role)
        }:
            continue
        if membership.environment_scope != ["*"]:
            continue
        patterns.append(authorization.workflow_scope_to_sql_like(membership.workflow_scope))
    return resolved_environment, patterns


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list — simple, deterministic,
    and dependency-free, appropriate for the bounded (window + scope limited)
    in-memory sample ``get_metrics`` computes over (ADR-019 section 4). Index is
    ``ceil(fraction * n) - 1``, clamped into range."""
    n = len(sorted_samples)
    index = max(0, min(n - 1, math.ceil(fraction * n) - 1))
    return sorted_samples[index]


def get_metrics(
    session: Session,
    *,
    principal_id: str,
    environment: str | None = None,
    window: str = "24h",
    group_by: str | None = None,
    enable_v2: bool = False,
) -> MetricsResult:
    """Bounded, authorization-filtered-before-aggregation operational metrics (stage
    08, MCP_TOOLS.md section 5.7, ADR-019). ``window`` is one of four enumerated
    values — never a caller-supplied arbitrary range (ADR-019 section 3, an
    arbitrarily narrow custom window could isolate a single operation). A latency
    percentile with fewer than 10 samples in the window is reported ``None`` with its
    own ``*_reason="insufficient_sample"`` rather than a number computed from too few
    points to mean anything (ADR-019 section 4). A ``group_by="workflow"`` breakdown
    beyond 50 distinct workflows folds the remainder into one ``"other"`` entry
    carrying only a count (ADR-019 section 3) — cardinality here is bounded by the
    registry's own distinct workflow count, never attacker-controlled, since a caller
    can create operations but never a new workflow id.
    """
    if window not in _METRICS_WINDOW_SECONDS:
        raise InvalidArgumentsError(details={"window": window})
    if group_by is not None and group_by not in {"workflow", "risk", "side_effects", "outcome"}:
        raise InvalidArgumentsError(details={"group_by": group_by})

    generated_at = datetime.now(UTC)
    since = generated_at - timedelta(seconds=_METRICS_WINDOW_SECONDS[window])
    resolved_environment, like_patterns = _resolve_scope(
        session,
        principal_id=principal_id,
        environment=environment,
        tool_name="get_metrics",
        enable_v2=enable_v2,
    )

    op_repo = OperationRepository(session)
    by_outcome = op_repo.count_by_outcome(
        workflow_id_like_patterns=like_patterns, environment=resolved_environment, since=since
    )
    totals = MetricsTotals(count=sum(by_outcome.values()), by_outcome=by_outcome)

    durations = sorted(
        ExecutionResultRepository(session).list_finished_durations_ms(
            workflow_id_like_patterns=like_patterns, environment=resolved_environment, since=since
        )
    )
    percentiles: dict[str, float | None] = {}
    reasons: dict[str, str | None] = {}
    for label, fraction in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        if len(durations) >= 10:
            percentiles[label] = round(_percentile(durations, fraction), 2)
            reasons[f"{label}_reason"] = None
        else:
            percentiles[label] = None
            reasons[f"{label}_reason"] = "insufficient_sample"
    latency_ms = LatencyPercentiles(
        p50=percentiles["p50"],
        p50_reason=reasons["p50_reason"],
        p95=percentiles["p95"],
        p95_reason=reasons["p95_reason"],
        p99=percentiles["p99"],
        p99_reason=reasons["p99_reason"],
    )

    breakdown: list[MetricsBreakdownEntry] = []
    if group_by == "outcome":
        breakdown = [
            MetricsBreakdownEntry(key=outcome, count=count) for outcome, count in by_outcome.items()
        ]
        breakdown.sort(key=lambda e: e.count, reverse=True)
    elif group_by == "workflow":
        rows = op_repo.breakdown_by_workflow(
            workflow_id_like_patterns=like_patterns, environment=resolved_environment, since=since
        )
        top, rest = rows[:50], rows[50:]
        breakdown = [
            MetricsBreakdownEntry(key=workflow_id, count=count, by_outcome=outcome_counts)
            for workflow_id, count, outcome_counts in top
        ]
        if rest:
            breakdown.append(
                MetricsBreakdownEntry(
                    key="other",
                    count=sum(count for _, count, _ in rest),
                    note=f"{len(rest)} additional workflows below the top-50 cutoff",
                )
            )
    elif group_by in ("risk", "side_effects"):
        document = _require_active_document(session)
        attribute_by_workflow = {
            entry.id: (entry.risk if group_by == "risk" else entry.side_effects)
            for entry in document.workflows
        }
        rows = op_repo.breakdown_by_workflow(
            workflow_id_like_patterns=like_patterns, environment=resolved_environment, since=since
        )
        grouped: dict[str, dict[str, int]] = {}
        for workflow_id, _count, outcome_counts in rows:
            key = attribute_by_workflow.get(workflow_id, "other")
            bucket = grouped.setdefault(key, {})
            for outcome, count in outcome_counts.items():
                bucket[outcome] = bucket.get(outcome, 0) + count
        breakdown = [
            MetricsBreakdownEntry(key=key, count=sum(counts.values()), by_outcome=counts)
            for key, counts in grouped.items()
        ]
        breakdown.sort(key=lambda e: e.count, reverse=True)

    return MetricsResult(
        environment=resolved_environment if enable_v2 else None,
        window=window,  # type: ignore[arg-type]
        generated_at=generated_at,
        totals=totals,
        latency_ms=latency_ms,
        breakdown=breakdown,
    )


def _encode_audit_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"seq": seq}).encode()).decode()


def _decode_audit_cursor(cursor: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(payload["seq"])
    except Exception as exc:
        raise InvalidArgumentsError(details={"cursor": cursor}) from exc


def list_audit_events(
    session: Session,
    *,
    principal_id: str,
    environment: str | None = None,
    workflow_id: str | None = None,
    since: datetime | None = None,
    limit: int = 20,
    cursor: str | None = None,
    enable_v2: bool = False,
) -> AuditEventPage:
    """Query the audit chain within the caller's authorization scope (stage 08,
    MCP_TOOLS.md section 5.8, ADR-012 section 3). Authorization filters the query,
    not the result: an entry whose subject resolves to a workflow or environment
    outside the caller's scope is excluded from the query entirely, never returned
    with a redacted ``detail`` — even the existence of an event for an unauthorized
    workflow is enumeration-adjacent information the caller has no standing to
    receive. ``detail`` carries the same write-time redaction v1 already applies; this
    adds no second redaction pass and no broader-role view of any entry's content.
    Cursor-paginated, anchored to ``audit_log.seq`` — never an offset, which would
    silently skip or duplicate rows over a concurrently-growing append-only log."""
    if not (1 <= limit <= 100):
        raise InvalidArgumentsError(details={"limit": limit})
    before_seq = _decode_audit_cursor(cursor) if cursor is not None else None

    resolved_environment, like_patterns = _resolve_scope(
        session,
        principal_id=principal_id,
        environment=environment,
        tool_name="list_audit_events",
        enable_v2=enable_v2,
    )
    include_registry_snapshot_events = True
    if enable_v2:
        memberships = OrganizationMembershipRepository(session).list_active_for_principal(
            principal_id
        )
        include_registry_snapshot_events = authorization.has_role(memberships, "admin")

    rows = AuditLogRepository(session).list_page(
        before_seq=before_seq,
        limit=limit,
        since=since,
        workflow_id=workflow_id,
        workflow_id_like_patterns=like_patterns,
        environment_id=resolved_environment,
        include_registry_snapshot_events=include_registry_snapshot_events,
    )
    events = [
        AuditEvent(
            seq=row.seq,
            prev_hash=row.prev_hash,
            entry_hash=row.entry_hash,
            occurred_at=row.occurred_at,
            actor=row.actor,
            action=row.action,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            outcome=row.outcome,  # type: ignore[arg-type]
            detail=row.detail,
        )
        for row in rows
    ]
    next_cursor = _encode_audit_cursor(events[-1].seq) if len(events) == limit else None
    return AuditEventPage(events=events, next_cursor=next_cursor)


def _alert_on_drift(
    session: Session, *, sink: NotificationSink, workflow_id: str, preflight_result: PreflightResult
) -> None:
    """The reactive half of stage 08's drift alert (BUILD_PLAN section 8) — fired the
    moment ``prepare_operation``/``retry_operation`` themselves discover
    ``DEFINITION_DRIFT``, rather than a periodic full-registry n8n-polling sweep this
    codebase has no other precedent for. ``subject_id`` folds in the *live* hash, not
    just the workflow id: the same drift persisting across many blocked ``prepare``
    attempts dedups to one alert (identical live hash -> identical idempotency key),
    but the workflow drifting *again* afterward, to a different live definition, is a
    new key and alerts again — this is what makes "repeated drift" (the stage prompt's
    own wording) mean "once per distinct drift", not "once ever" or "once per blocked
    attempt"."""
    drift_check = next(
        (
            c
            for c in preflight_result.checks
            if c.check == "definition_unchanged"
            and c.status == "fail"
            and c.code == DefinitionDriftError.code
        ),
        None,
    )
    if drift_check is None or not isinstance(drift_check.detail, dict):
        return
    live_hash = drift_check.detail.get("live")
    if not live_hash:
        return
    event = NotificationEvent(
        event_type="drift.detected",
        subject_type="workflow",
        subject_id=f"{workflow_id}:{live_hash}",
        principal_id=None,
        occurred_at=datetime.now(UTC),
        fetch_reference=f"n8n-operator registry diff-live {workflow_id}",
    )
    _deliver_with_dedup(session, sink=sink, event=event)


def check_and_deliver_alerts(
    session: Session, *, sink: NotificationSink, executing_stuck_threshold_seconds: int = 3600
) -> int:
    """Alert-hook sweep for the two conditions that need periodic detection rather
    than a reactive hook (stage 08, BUILD_PLAN section 8): an ``EXECUTING`` operation
    stuck past a threshold, and an operation that has reached ``UNKNOWN``. Mirrors
    ``expire_overdue_operations``/``retry_failed_notifications``'s own "swept
    reconciliation, safe to run on a timer, idempotent" shape exactly — re-scanning
    every matching row on every tick is safe because ``_deliver_with_dedup``'s
    permanent per-``(subject_id, event_type)`` dedup, not this function, is what
    keeps an already-alerted operation from alerting again. Returns the count of
    deliveries that succeeded on this sweep, mirroring
    ``retry_failed_notifications``'s own return contract.

    Drift detection is deliberately *not* part of this sweep — see
    ``_prepare_or_retry``'s own reactive drift-alert hook, fired the moment
    ``prepare_operation``/``retry_operation`` already discover drift, rather than a
    second, redundant n8n-polling mechanism this codebase has nowhere else.

    A receipt's own ``delivered`` field is ``True`` both for a fresh, successful
    delivery *and* for a deduplicated "already delivered" lookup that never called
    the sink at all (:func:`_deliver_with_dedup`'s own contract) — this function's
    return value counts only the former (``receipt.detail != _ALREADY_DELIVERED_DETAIL``),
    matching "count of deliveries that succeeded *on this sweep*", not "count of
    events that are, as of now, in a delivered state".
    """
    threshold = datetime.now(UTC) - timedelta(seconds=executing_stuck_threshold_seconds)
    op_repo = OperationRepository(session)
    delivered = 0
    for row in op_repo.stuck_executing(older_than=threshold):
        event = NotificationEvent(
            event_type="operation.stuck",
            subject_type="operation",
            subject_id=row.id,
            principal_id=None,
            occurred_at=datetime.now(UTC),
            fetch_reference=f"n8n-operator operations show {row.id}",
        )
        receipt = _deliver_with_dedup(session, sink=sink, event=event)
        if receipt.delivered and receipt.detail != _ALREADY_DELIVERED_DETAIL:
            delivered += 1
    for row in op_repo.list_unknown():
        event = NotificationEvent(
            event_type="operation.unknown",
            subject_type="operation",
            subject_id=row.id,
            principal_id=None,
            occurred_at=datetime.now(UTC),
            fetch_reference=f"n8n-operator operations show {row.id}",
        )
        receipt = _deliver_with_dedup(session, sink=sink, event=event)
        if receipt.delivered and receipt.detail != _ALREADY_DELIVERED_DETAIL:
            delivered += 1
    return delivered


def get_instance_health(health: HealthPort) -> HealthCheckResult:
    """Whether the configured n8n instance is reachable (MCP_TOOLS.md section 2.3).

    Unlike every other use case here, this touches neither the registry nor storage —
    reachability is a property of the n8n instance alone, not of any one workflow or
    operation — so it takes no ``Session``. A thin pass-through over ``health.check()``,
    kept as a named use case (rather than the MCP adapter calling the port directly) so
    the "MCP calls core.service" rule (ADR-001) has no exception.

    Not authorization-gated in Stage 03: MCP_TOOLS.md section 5.9's v2 form for this
    tool is *environment*-scoped only ("unauthorized environment → ENVIRONMENT_NOT_FOUND"),
    with no workflow to check a role/workflow-scope grant against — and no v1 tool
    carries an ``environment`` argument yet (Stage 04's charter, this module's own
    ``_authorize`` docstring). Every role already includes this tool in ADR-015's
    matrix, so an authenticated v2 caller with any active membership anywhere would
    pass regardless; wiring the check now would add a database round trip for a
    decision that's Stage 04's to make correctly, not Stage 03's to approximate.
    """
    return health.check()


# ----------------------------------------------------------------------------------
# The operation lifecycle (BUILD_PLAN sections 5, 8.1; ARCHITECTURE.md section 4).
# ----------------------------------------------------------------------------------


MAX_RETRY_CHAIN_DEPTH = 10
"""Stage 06 (ADR-012 section 1): the deepest a retry-of-a-retry-of-a-retry chain may
grow. A cycle is structurally impossible (a child's ``parent_operation_id`` always
names an operation that already existed when the child was created, and is never
mutated afterward — the lineage graph is a DAG by construction), but nothing else
bounds how many times a chain can be extended; unbounded growth is the concern this
constant exists to cap, not a cycle (relevant to threat L-04, retry storms)."""


def prepare_operation(
    session: Session,
    *,
    principal_id: str,
    environment: str | None = None,
    workflow_id: str,
    arguments: dict[str, Any],
    preflight: PreflightPort,
    server_max_argument_bytes: int,
    idempotency_key: str | None = None,
    reason: str | None = None,
    enable_v2: bool = False,
    notification_sink: NotificationSink | None = None,
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

    Stage 06: everything from workflow/environment resolution onward is shared with
    :func:`retry_operation` via :func:`_prepare_or_retry` — this function's own job is
    only to resolve *its own* ``workflow_id``/``environment`` arguments (a retry
    resolves the parent's instead) before delegating.
    """
    snapshot = _require_active_snapshot(session)
    document = RegistryDocument.model_validate(snapshot.document)
    entry = _require_enabled_entry(document, workflow_id)
    assert entry.approval is not None  # resolved entries always carry a concrete value

    resolved_environment_id, entry = _apply_environment(
        session,
        principal_id=principal_id,
        environment=environment,
        workflow_id=workflow_id,
        base_entry=entry,
        tool_name="prepare_operation",
        enable_v2=enable_v2,
        forbid_archived=True,
    )
    # The legacy free-text idempotency-namespace column (ADR-011's `environment` — v1
    # unchanged: whatever the caller passed, or "default"). In v2 it becomes the
    # resolved environment's own id, so the same idempotency key against two different
    # environments never collides (Stage 04's own named test scenario).
    environment_value = (
        resolved_environment_id
        if resolved_environment_id is not None
        else (environment if environment is not None else "default")
    )

    return _prepare_or_retry(
        session,
        principal_id=principal_id,
        workflow_id=workflow_id,
        arguments=arguments,
        environment_value=environment_value,
        resolved_environment_id=resolved_environment_id,
        entry=entry,
        snapshot=snapshot,
        preflight=preflight,
        server_max_argument_bytes=server_max_argument_bytes,
        idempotency_key=idempotency_key,
        reason=reason,
        enable_v2=enable_v2,
        notification_sink=notification_sink,
    )


def _prepare_or_retry(
    session: Session,
    *,
    principal_id: str,
    workflow_id: str,
    arguments: dict[str, Any],
    environment_value: str,
    resolved_environment_id: str | None,
    entry: WorkflowEntry,
    snapshot: RegistrySnapshot,
    preflight: PreflightPort,
    server_max_argument_bytes: int,
    idempotency_key: str | None,
    reason: str | None,
    enable_v2: bool,
    parent_operation_id: str | None = None,
    notification_sink: NotificationSink | None = None,
) -> tuple[Operation, bool, str | None]:
    """The shared body of :func:`prepare_operation` and :func:`retry_operation` —
    argument-size/rate-limit checks, idempotency, creation, validation, preflight, and
    approval-or-auto-approve — parameterized only by whether this new operation has a
    parent. See :func:`prepare_operation`'s own docstring for the full return-shape
    and result-vs-exception contract; nothing about that contract differs by caller.

    ``notification_sink`` (stage 08, ADR-018): when given, a preflight block on
    ``DEFINITION_DRIFT`` fires a reactive ``drift.detected`` alert (see the
    ``if not preflight_result.ready:`` branch below) — ``None`` (v1, and v2 before a
    sink is configured) is a pure no-op, identical to how ``request_approval``'s own
    sink dependency already works.
    """
    canonical_bytes = canonicalize_arguments(arguments)
    fingerprint = fingerprint_arguments(canonical_bytes)
    effective_limit = entry.limits.max_argument_bytes or server_max_argument_bytes
    op_repo = OperationRepository(session)

    try:
        check_argument_size(canonical_bytes, effective_limit=effective_limit)
        if entry.limits.rate_limit_per_minute is not None:
            window_start = datetime.now(UTC) - timedelta(seconds=60)
            recent_count = op_repo.count_recent(workflow_id=workflow_id, since=window_start)
            if recent_count >= entry.limits.rate_limit_per_minute:
                raise RateLimitedError(
                    details={
                        "limit_per_minute": entry.limits.rate_limit_per_minute,
                        "recent_count": recent_count,
                    }
                )
    except (ArgumentsTooLargeError, RateLimitedError) as exc:
        audit_writer.write(
            AuditLogRepository(session),
            actor=principal_id,
            action="operation.prepare_denied",
            subject_type="workflow",
            subject_id=workflow_id,
            outcome="denied",
            detail={"code": exc.code, **exc.details},
        )
        # ADR-011: "the attempt is still audited" even though no operation row is ever
        # written. The caller's own `session_scope` rolls back on any exception
        # propagating out of it (storage/session.py) — which would silently take this
        # audit entry down with it, since nothing else in this call wrote anything to
        # roll back *from*. Committing here, before raising, ends the current
        # transaction with only the audit entry in it; the caller's subsequent rollback
        # then has nothing left to undo. Applied identically to a rate-limit refusal
        # (MCP_TOOLS.md section 2.6): recording the request is what's being refused
        # either way, so neither writes an operation row.
        session.commit()
        raise

    # A retry's idempotency key is scoped additionally to its parent (MCP_TOOLS.md
    # section 5.5) by folding the parent's ID into the *stored* key value itself —
    # never into the unique constraint's own columns (see `storage/models.py`'s
    # `Operation` docstring for why widening the constraint with a nullable column
    # would have been wrong). Internal only: never echoed back to any caller, since
    # `idempotency_key` is not itself part of any read result today.
    namespaced_idempotency_key = (
        f"retry:{parent_operation_id}:{idempotency_key}"
        if idempotency_key is not None and parent_operation_id is not None
        else idempotency_key
    )

    if namespaced_idempotency_key is not None:
        existing = op_repo.find_by_idempotency(
            principal_id=principal_id,
            environment=environment_value,
            workflow_id=workflow_id,
            idempotency_key=namespaced_idempotency_key,
        )
        resolution = resolve_idempotency(
            existing_fingerprint=existing.argument_fingerprint if existing else None,
            new_fingerprint=fingerprint,
        )
        if resolution is IdempotencyResolution.REPLAY:
            assert existing is not None
            existing = _apply_lazy_expiry(session, existing)
            return _to_domain(existing), True, None

    resolved_organization_id: str | None = None
    if enable_v2 and resolved_environment_id is not None:
        resolved_environment_row = EnvironmentRepository(session).get(resolved_environment_id)
        assert resolved_environment_row is not None  # just resolved above; cannot vanish mid-call
        resolved_organization_id = resolved_environment_row.organization_id

    handle = mint_operation_handle()
    try:
        operation = op_repo.create(
            id=handle,
            principal_id=principal_id,
            environment=environment_value,
            environment_id=resolved_environment_id,
            organization_id=resolved_organization_id,
            snapshot_id=snapshot.id,
            workflow_id=workflow_id,
            definition_hash=entry.definition_hash,
            state="PREPARING",
            # Raw, not redacted: phase 7's dispatch needs the real values (redacting
            # the email before sending it to n8n would break the workflow), and the
            # fingerprint re-verified at execute time (`execute_operation`) is
            # computed over these same raw arguments, matching the one taken at
            # prepare time (invariant I5). Redaction happens at the *read* boundary
            # instead — wherever arguments are echoed to a caller (`get_operation`,
            # `get_approval_decision_context`) — never at rest.
            arguments=arguments,
            argument_fingerprint=fingerprint,
            argument_bytes=len(canonical_bytes),
            idempotency_key=namespaced_idempotency_key,
            parent_operation_id=parent_operation_id,
        )
    except IntegrityError:
        # AC-50's concurrent-retry race: two callers with the same idempotency key
        # (and, for a retry, the same parent — already folded into
        # `namespaced_idempotency_key` above) both passed the SELECT above because
        # neither had committed yet. Nothing else has been written in this
        # transaction — the checks above are all reads — so a plain rollback is safe;
        # a fresh lookup now sees whichever caller's INSERT actually won and returns
        # it as a replay, exactly like the pre-existing SELECT-hit path above does.
        # Only reachable with a real key: a `None` key never round-trips through the
        # unique constraint at all (NULL is never equal to NULL there either).
        if namespaced_idempotency_key is None:
            raise
        session.rollback()
        existing = op_repo.find_by_idempotency(
            principal_id=principal_id,
            environment=environment_value,
            workflow_id=workflow_id,
            idempotency_key=namespaced_idempotency_key,
        )
        assert existing is not None  # the constraint that just fired names this exact row
        existing = _apply_lazy_expiry(session, existing)
        return _to_domain(existing), True, None

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
        if notification_sink is not None:
            _alert_on_drift(
                session,
                sink=notification_sink,
                workflow_id=workflow_id,
                preflight_result=preflight_result,
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
        field_updates: dict[str, Any] = {"approval_expires_at": approval_expires_at}
        if enable_v2:
            # Stage 05 (ADR-017 section 1): the eligible-approver snapshot is
            # computed and frozen right here, at T04 entry — never recomputed, never
            # re-expanded by a later membership change (invariant I13, AC-40).
            assert resolved_organization_id is not None  # every v2 operation has one
            assert resolved_environment_id is not None
            eligible_approvers = _compute_eligible_approvers(
                session,
                organization_id=resolved_organization_id,
                workflow_id=workflow_id,
                environment_id=resolved_environment_id,
                requester_principal_id=principal_id,
            )
            field_updates["approval_policy_snapshot"] = {
                "quorum_count": entry.limits.quorum_count,
                "eligible_approvers": eligible_approvers,
            }
        operation = _apply_and_audit(
            session,
            operation,
            "T04",
            actor="system",
            detail=checks_detail,
            **field_updates,
        )
        if not enable_v2:
            # v1 only: one shared token, minted immediately, returned to the caller —
            # byte-identical to every pre-stage-05 behavior. v2's eligible approvers
            # exclude the requester by construction, so a token returned to *them*
            # here would never be usable — v2 callers mint per-approver tokens
            # lazily, in `request_approval`, once notification actually routes.
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


def retry_operation(
    session: Session,
    *,
    operation_id: str,
    principal_id: str,
    preflight: PreflightPort,
    server_max_argument_bytes: int,
    idempotency_key: str | None = None,
    reason: str | None = None,
    enable_v2: bool = False,
    notification_sink: NotificationSink | None = None,
) -> tuple[Operation, bool, str | None]:
    """Governed retry (MCP_TOOLS.md section 5.5, ADR-005/ADR-012, invariant I11): a
    brand-new operation, linked by ``parent_operation_id``, with its ``workflow_id``
    and ``arguments`` taken from the parent (never from this call — there is no such
    argument) but validation, preflight, and approval all recalculated from scratch
    against whatever is current *now*. The parent is only ever read here — never
    transitioned, never re-approved, never handed a new handle; its own row is
    unreachable by anything this function does after step 1 below.

    Returns the same ``(operation, idempotent_replay, approval_token)`` shape
    :func:`prepare_operation` does. ``SUCCEEDED`` is never returned directly — a retry
    that reaches ``APPROVED`` still needs its own ``execute_operation`` call, exactly
    like any other operation.
    """
    parent = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="retry_operation", enable_v2=enable_v2
    )
    parent = _apply_lazy_expiry(session, parent)
    if parent.state not in {"FAILED", "UNKNOWN", "BLOCKED", "EXPIRED", "REJECTED"}:
        raise RetryNotApplicableError(details={"parent_state": parent.state})

    depth = 0
    ancestor_id = parent.parent_operation_id
    op_repo = OperationRepository(session)
    while ancestor_id is not None:
        depth += 1
        if depth >= MAX_RETRY_CHAIN_DEPTH:
            raise RetryNotApplicableError(
                details={"reason": "chain_depth_exceeded", "limit": MAX_RETRY_CHAIN_DEPTH}
            )
        ancestor = op_repo.get(ancestor_id)
        assert ancestor is not None  # parent_operation_id never names a deleted row
        ancestor_id = ancestor.parent_operation_id

    snapshot = _require_active_snapshot(session)
    document = RegistryDocument.model_validate(snapshot.document)
    entry = _require_enabled_entry(document, parent.workflow_id)
    assert entry.approval is not None

    resolved_environment_id, entry = _apply_environment(
        session,
        principal_id=principal_id,
        environment=parent.environment_id,
        workflow_id=parent.workflow_id,
        base_entry=entry,
        tool_name="retry_operation",
        enable_v2=enable_v2,
        forbid_archived=True,
    )
    environment_value = (
        resolved_environment_id if resolved_environment_id is not None else parent.environment
    )

    return _prepare_or_retry(
        session,
        principal_id=principal_id,
        workflow_id=parent.workflow_id,
        arguments=parent.arguments,
        environment_value=environment_value,
        resolved_environment_id=resolved_environment_id,
        entry=entry,
        snapshot=snapshot,
        preflight=preflight,
        server_max_argument_bytes=server_max_argument_bytes,
        idempotency_key=idempotency_key,
        reason=reason,
        enable_v2=enable_v2,
        parent_operation_id=parent.id,
        notification_sink=notification_sink,
    )


_RECONCILIATION_ACTION = "operation.reconciliation_recorded"


def reconcile_operation(
    session: Session,
    *,
    operation_id: str,
    principal_id: str,
    execution_id: str,
    note: str,
    reconciliation: ReconciliationPort,
    enable_v2: bool = False,
) -> ReconciliationRecord:
    """Record exact-ID reconciliation evidence for an ``UNKNOWN`` operation (stage 06,
    ADR-009/ADR-012) — an ``audit_log`` annotation only, never a state transition;
    ``operations.state`` is untouched by this function, before and after (invariant
    I7 — ``UNKNOWN`` has no outgoing edge in ``state_machine.TRANSITIONS`` at all, and
    this function never calls it). CLI-only: no MCP tool reaches this (boundary B4's
    spirit — a human confirms external evidence, never an agent).

    Unlike most gated use cases, this one does **not** use
    :func:`_get_owned_operation_row`'s owner-sees-their-own-work shortcut: recording
    reconciliation evidence is gated on :data:`authorization.RECONCILE_CAPABILITY`
    (``admin`` only) regardless of who originally requested the operation — the
    requester having prepared it is not, by itself, license to assert what n8n did
    with it. v1 (``enable_v2=False``) keeps the ownership-only rule every other v1
    path already has, since v1 has no role concept to gate by at all.
    """
    row = _get_operation_row(session, operation_id)
    if enable_v2:
        environment_organization_id: str | None = None
        if row.environment_id is not None:
            environment_row = EnvironmentRepository(session).get(row.environment_id)
            assert environment_row is not None
            environment_organization_id = environment_row.organization_id
        decision = _authorize(
            session,
            principal_id=principal_id,
            tool_name=authorization.RECONCILE_CAPABILITY,
            workflow_id=row.workflow_id,
            environment_id=row.environment_id,
            environment_organization_id=environment_organization_id,
            enable_v2=enable_v2,
        )
        if not decision.allowed:
            raise OperationNotFoundError()
    elif row.principal_id != principal_id:
        raise OperationNotFoundError()

    if row.state != "UNKNOWN":
        raise ReconciliationNotApplicableError(details={"state": row.state})

    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    try:
        execution = reconciliation.get_execution(execution_id)
    except OperatorError as exc:
        # Never inferred from silence or elapsed time — a lookup failure (not found,
        # instance unreachable, malformed record) is a refusal to record anything,
        # exactly as stale n8n history or a missing correlation ID must be.
        raise ReconciliationNotApplicableError(
            details={"execution_id": execution_id, "lookup_error": exc.code}
        ) from exc
    if execution.n8n_workflow_id != entry.n8n_workflow_id:
        raise ReconciliationNotApplicableError(
            details={
                "reason": "execution_belongs_to_a_different_workflow",
                "execution_workflow_id": execution.n8n_workflow_id,
                "expected_n8n_workflow_id": entry.n8n_workflow_id,
            }
        )

    occurred_at = datetime.now(UTC)
    audit_writer.write(
        AuditLogRepository(session),
        actor=principal_id,
        action=_RECONCILIATION_ACTION,
        subject_type="operation",
        subject_id=operation_id,
        outcome="allowed",
        detail={
            "execution_id": execution_id,
            "n8n_workflow_id": execution.n8n_workflow_id,
            "n8n_execution_status": execution.status,
            "note": note,
        },
        occurred_at=occurred_at,
    )
    return ReconciliationRecord(
        operation_id=operation_id,
        execution_id=execution_id,
        n8n_workflow_id=execution.n8n_workflow_id,
        n8n_execution_status=execution.status,
        note=note,
        actor=principal_id,
        recorded_at=occurred_at,
    )


def list_reconciliation_events(
    session: Session, *, operation_id: str, principal_id: str, enable_v2: bool = False
) -> list[ReconciliationRecord]:
    """Every reconciliation annotation recorded for one operation, oldest first — a
    read, gated the same way any other operation read is (whoever can already see the
    operation may see its reconciliation history; the ``admin``-only gate is on
    *recording* evidence, in :func:`reconcile_operation`, not on reading it)."""
    _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="get_operation", enable_v2=enable_v2
    )
    entries = AuditLogRepository(session).list_for_subject(
        subject_type="operation", subject_id=operation_id
    )
    return [
        ReconciliationRecord(
            operation_id=operation_id,
            execution_id=entry.detail["execution_id"],
            n8n_workflow_id=entry.detail["n8n_workflow_id"],
            n8n_execution_status=entry.detail["n8n_execution_status"],
            note=entry.detail["note"],
            actor=entry.actor,
            recorded_at=entry.occurred_at,
        )
        for entry in entries
        if entry.action == _RECONCILIATION_ACTION
    ]


def _v2_quorum_snapshot(row: OperationRow) -> dict[str, Any] | None:
    """``row.approval_policy_snapshot`` if this operation actually went through the
    v2 T04 path (a real dict with both keys) — ``None`` otherwise, including the
    defensive case where ``enable_v2`` was flipped between prepare and decide time
    (a deployment misconfiguration, not something a caller can cause), in which case
    every v2 caller falls back to the exact v1 shape rather than crashing on a
    missing snapshot."""
    snapshot = row.approval_policy_snapshot
    if not snapshot or "eligible_approvers" not in snapshot or "quorum_count" not in snapshot:
        return None
    return snapshot


def _get_or_mint_own_approval_row(
    session: Session, row: OperationRow, *, decided_by: str
) -> ApprovalRow:
    """This decider's own row for ``row``'s operation — the one
    ``request_approval`` already minted for them (``assigned_to``), or, if they are
    deciding directly (CLI, no prior routing call), a freshly minted one. The token
    on a freshly minted row here is never handed to anyone — deciding through the
    CLI has no token to present — it exists only to satisfy this table's
    ``NOT NULL``/unique ``token_hash`` invariant uniformly, the same way a v1 row
    always has one whether or not a human ever clicked the link."""
    existing = ApprovalRepository(session).get_by_operation_and_decider(row.id, decided_by)
    if existing is not None:
        return existing
    minted = mint_approval_token()
    binding_hash = compute_approval_binding(
        operation_id=row.id,
        principal_id=decided_by,
        argument_fingerprint=row.argument_fingerprint,
        snapshot_id=row.snapshot_id,
        definition_hash=row.definition_hash,
    )
    assert row.approval_expires_at is not None  # every PENDING_APPROVAL row has one
    return ApprovalRepository(session).create(
        operation_id=row.id,
        token_hash=minted.token_hash,
        binding_hash=binding_hash,
        expires_at=row.approval_expires_at,
        assigned_to=decided_by,
    )


def approve_operation(
    session: Session,
    *,
    operation_id: str,
    decided_by: str,
    client_fingerprint: str | None = None,
    enable_v2: bool = False,
) -> Operation:
    """T06: a human approves (ADR-010, ADR-017; both the CLI and the approval-page
    channel call this one use case). Not scoped to a preparing principal in v1's
    single-principal model, where the approver and preparer are always the same
    ``local`` identity.

    v2 (``enable_v2=True``): ``decided_by`` may never be the operation's own
    requester (structurally excluded from the snapshot to begin with, ADR-017
    section 1 — checked again here regardless, defense in depth), must be a member
    of the operation's own frozen ``approval_policy_snapshot`` (never a *live*
    role/scope re-check — the policy in force at ``PENDING_APPROVAL`` entry is what
    governs, invariant I13, not whatever it drifted to since), and must not have
    already decided (:class:`~n8n_operator.errors.ApprovalAlreadyDecidedError`).
    Every denial except the last raises the identical
    :class:`~n8n_operator.errors.OperationNotFoundError` a nonexistent operation ID
    would (invariant I14). Reaching quorum (``quorum_count`` approvals, zero
    rejections — always true here, since a reject already moved the operation to
    ``REJECTED`` and this call would have failed the state check) applies T06; short
    of quorum, the vote is recorded and the operation stays ``PENDING_APPROVAL``.

    ``client_fingerprint`` is coarse request provenance for the audit trail
    (BUILD_PLAN section 8.1) — set by the web approval channel, left ``None`` by the
    CLI, which has no request to fingerprint.
    """
    row = _get_operation_row_for_update(session, operation_id)
    snapshot = _v2_quorum_snapshot(row) if enable_v2 else None
    if snapshot is None:
        decision = _authorize(
            session,
            principal_id=decided_by,
            tool_name=authorization.APPROVE_REJECT_CAPABILITY,
            workflow_id=row.workflow_id,
            enable_v2=enable_v2,
            requester_principal_id=row.principal_id,
            decider_principal_id=decided_by,
        )
        if not decision.allowed:
            raise OperationNotFoundError()
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

    if decided_by == row.principal_id or decided_by not in snapshot["eligible_approvers"]:
        raise OperationNotFoundError()
    own_row = _get_or_mint_own_approval_row(session, row, decided_by=decided_by)
    if own_row.decision is not None:
        raise ApprovalAlreadyDecidedError()
    ApprovalRepository(session).record_decision(
        approval_id=own_row.id,
        decision="approved",
        decided_by=decided_by,
        client_fingerprint=client_fingerprint,
    )
    approved_count = sum(
        1
        for a in ApprovalRepository(session).list_for_operation(operation_id)
        if a.decision == "approved"
    )
    if approved_count < snapshot["quorum_count"]:
        return _to_domain(row)
    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    execution_deadline = datetime.now(UTC) + timedelta(
        seconds=_resolved_ttl(entry.limits.execution_ttl_seconds)
    )
    updated = _apply_and_audit(
        session, row, "T06", actor=decided_by, execution_deadline=execution_deadline
    )
    return _to_domain(updated)


def reject_operation(
    session: Session,
    *,
    operation_id: str,
    decided_by: str,
    client_fingerprint: str | None = None,
    enable_v2: bool = False,
) -> Operation:
    """T07: a human rejects (ADR-010, ADR-017 section 2 — one reject is final,
    regardless of how many approvals are already in; no tallying, unlike
    :func:`approve_operation`). ``client_fingerprint``/authorization as that
    function."""
    row = _get_operation_row_for_update(session, operation_id)
    snapshot = _v2_quorum_snapshot(row) if enable_v2 else None
    if snapshot is None:
        decision = _authorize(
            session,
            principal_id=decided_by,
            tool_name=authorization.APPROVE_REJECT_CAPABILITY,
            workflow_id=row.workflow_id,
            enable_v2=enable_v2,
            requester_principal_id=row.principal_id,
            decider_principal_id=decided_by,
        )
        if not decision.allowed:
            raise OperationNotFoundError()
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

    if decided_by == row.principal_id or decided_by not in snapshot["eligible_approvers"]:
        raise OperationNotFoundError()
    own_row = _get_or_mint_own_approval_row(session, row, decided_by=decided_by)
    if own_row.decision is not None:
        raise ApprovalAlreadyDecidedError()
    ApprovalRepository(session).record_decision(
        approval_id=own_row.id,
        decision="rejected",
        decided_by=decided_by,
        client_fingerprint=client_fingerprint,
    )
    updated = _apply_and_audit(session, row, "T07", actor=decided_by)
    return _to_domain(updated)


def _approval_decision_context(
    session: Session, row: OperationRow, approval_row: ApprovalRow | None
) -> ApprovalDecisionContext:
    """Build the shared decision-surface shape from an already-fetched, already-lazily-
    expired operation row. Not exported — every public entry point below fetches and
    lazy-expires the row its own way (by operation ID, by token, or via
    ``request_approval``/``get_approval_status``), then shares this one assembly
    step, so none of them can ever render the workflow/drift/deadline/quorum fields
    differently.

    ``approval_row``, when given, is *this specific caller's own* row (a token's
    assigned approver, or a CLI caller's own decision) — it drives the legacy scalar
    fields (``decision``/``decided_by``/``decided_at``/``assigned_to``) only. The
    quorum-wide fields (``quorum_count``/``decisions``/``outstanding_approvers``) are
    independent of it, computed straight from ``row``'s own frozen
    ``approval_policy_snapshot`` and every ``Approval`` row for the operation — v1
    (no snapshot) leaves them at their defaults (``quorum_count=1``, empty lists).
    """
    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    current_document = _require_active_document(session)
    current_entry = _find_entry(current_document, row.workflow_id)
    current_hash = current_entry.definition_hash if current_entry is not None else None

    snapshot = _v2_quorum_snapshot(row)
    quorum_count = snapshot["quorum_count"] if snapshot is not None else 1
    decisions: list[ApprovalDecisionEntry] = []
    outstanding_approvers: list[str] = []
    if snapshot is not None:
        decided_principals: set[str] = set()
        for a in ApprovalRepository(session).list_for_operation(row.id):
            if a.decision is not None and a.decided_by is not None and a.decided_at is not None:
                decisions.append(
                    ApprovalDecisionEntry(
                        principal_id=a.decided_by,
                        decision=a.decision,  # type: ignore[arg-type]
                        decided_at=a.decided_at,
                    )
                )
                decided_principals.add(a.decided_by)
        if not any(d.decision == "rejected" for d in decisions):
            outstanding_approvers = [
                p for p in snapshot["eligible_approvers"] if p not in decided_principals
            ]

    return ApprovalDecisionContext(
        operation_id=row.id,
        workflow_id=row.workflow_id,
        title=entry.title,
        description=entry.description,
        risk=entry.risk,
        side_effects=entry.side_effects,
        state=row.state,
        arguments=redact(row.arguments, entry.output.redact),
        registered_definition_hash=row.definition_hash,
        current_definition_hash=current_hash,
        drifted=current_hash != row.definition_hash,
        created_at=row.created_at,
        approval_expires_at=row.approval_expires_at,
        execution_deadline=row.execution_deadline,
        approval_required=approval_row is not None or snapshot is not None,
        decided=approval_row is not None and approval_row.decision is not None,
        decision=approval_row.decision if approval_row is not None else None,  # type: ignore[arg-type]
        decided_at=approval_row.decided_at if approval_row is not None else None,
        decided_by=approval_row.decided_by if approval_row is not None else None,
        assigned_to=approval_row.assigned_to if approval_row is not None else None,
        quorum_count=quorum_count,
        decisions=decisions,
        outstanding_approvers=outstanding_approvers,
        parent_operation_id=row.parent_operation_id,
    )


def get_approval_decision_context(
    session: Session, *, operation_id: str, principal_id: str, enable_v2: bool = False
) -> ApprovalDecisionContext:
    """Everything needed to render or review an approval decision by operation ID
    (ADR-010) — the CLI's ``operations approve``/``reject`` (before confirming) and
    ``operations approval-status`` both call this.

    v2 quorum mode: the scalar decision fields reflect *this caller's own* row
    (``ApprovalRepository.get_by_operation_and_decider``) — pending, decided, or
    absent if they were never routed a slot — never an arbitrary other approver's.
    v1: unchanged, the operation's one shared row regardless of caller.
    """
    row = _get_owned_operation_row(
        session,
        operation_id,
        principal_id,
        tool_name="get_approval_status",
        enable_v2=enable_v2,
    )
    if enable_v2 and _v2_quorum_snapshot(row) is not None:
        approval_row = ApprovalRepository(session).get_by_operation_and_decider(
            operation_id, principal_id
        )
    else:
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

    # v1 (approval_row.assigned_to is None): bound to the requester, exactly as
    # every pre-stage-05 token was. v2 (a per-approver token): bound to the specific
    # approver it was minted for — a token minted for one eligible approver can never
    # verify as another's, structurally, not by a runtime "is this the right person"
    # check (closes the forged-token/cross-approver-reuse edge case).
    binding_principal_id = approval_row.assigned_to or row.principal_id
    expected_binding = compute_approval_binding(
        operation_id=row.id,
        principal_id=binding_principal_id,
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


_ALREADY_DELIVERED_DETAIL = "already delivered (deduplicated)"


def _deliver_with_dedup(
    session: Session, *, sink: NotificationSink, event: NotificationEvent
) -> DeliveryReceipt:
    """The one place ``NotificationSink.deliver`` is ever called (ADR-018 section 2)
    — computes the idempotency key, and if a ``delivered`` row already exists for
    it, returns that receipt **without calling the sink again**: this is what makes
    ``request_approval`` called twice, or the same event handed to this function
    twice by operator error, produce exactly one received notification. A fresh
    attempt is recorded ``delivered`` or left ``pending`` (never retried inline —
    :func:`retry_failed_notifications` is the bounded, swept retry path, the same
    "lazy expiry now, sweep later" shape ``expire_overdue_operations`` already has
    for operations, applied here to notifications).
    """
    idempotency_key = f"{event.subject_id}:{event.principal_id or ''}:{event.event_type}"
    repo = NotificationDeliveryRepository(session)
    existing = repo.get_by_idempotency_key(idempotency_key)
    if existing is not None and existing.status == "delivered":
        return DeliveryReceipt(
            idempotency_key=idempotency_key,
            delivered=True,
            attempts=existing.attempts,
            status="delivered",
            detail=_ALREADY_DELIVERED_DETAIL,
        )
    delivery = existing or repo.create(
        idempotency_key=idempotency_key,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        event_type=event.event_type,
        principal_id=event.principal_id,
    )
    try:
        receipt = sink.deliver(event)
        delivered = receipt.delivered
    except Exception as exc:
        delivered = False
        _logger.warning(
            "notification_delivery_failed",
            extra={
                "idempotency_key": idempotency_key,
                "event_type": event.event_type,
                "error": str(exc),
            },
        )
    updated = repo.record_attempt(delivery.id, delivered=delivered)
    return DeliveryReceipt(
        idempotency_key=idempotency_key,
        delivered=delivered,
        attempts=updated.attempts,
        status=updated.status,  # type: ignore[arg-type]
    )


def retry_failed_notifications(
    session: Session, *, sink: NotificationSink, max_attempts: int = 5
) -> int:
    """Re-attempt every ``pending`` delivery, oldest/least-attempted first
    (``NotificationDeliveryRepository.list_pending``'s own ordering). A delivery
    that exhausts ``max_attempts`` becomes ``DELIVERY_FAILED`` — fail-visible, never
    retried again (ADR-018 section 2) — rather than retried forever. Returns the
    count of deliveries that succeeded on this sweep. Mirrors
    ``expire_overdue_operations``'s own "swept reconciliation, safe to run on a
    timer, idempotent" shape exactly.
    """
    repo = NotificationDeliveryRepository(session)
    delivered_count = 0
    for delivery in repo.list_pending():
        event = NotificationEvent(
            event_type=delivery.event_type,
            subject_type=delivery.subject_type,
            subject_id=delivery.subject_id,
            principal_id=delivery.principal_id,
            occurred_at=delivery.last_attempted_at or datetime.now(UTC),
            fetch_reference=f"n8n-operator operations approval-status {delivery.subject_id}",
        )
        try:
            receipt = sink.deliver(event)
            delivered = receipt.delivered
        except Exception as exc:
            delivered = False
            _logger.warning(
                "notification_delivery_retry_failed",
                extra={"delivery_id": delivery.id, "error": str(exc)},
            )
        updated = repo.record_attempt(delivery.id, delivered=delivered)
        if delivered:
            delivered_count += 1
        elif updated.attempts >= max_attempts:
            repo.mark_failed(delivery.id)
    return delivered_count


def request_approval(
    session: Session,
    *,
    operation_id: str,
    principal_id: str,
    sink: NotificationSink,
    approvers: list[str] | None = None,
    message: str | None = None,
    enable_v2: bool = True,
) -> RequestApprovalResult:
    """Route a ``PENDING_APPROVAL`` operation's approval to its eligible approvers
    and (re)send notifications (MCP_TOOLS.md section 5.3, ADR-017/018). **Still
    cannot grant approval** — the out-of-band decision itself crosses only the CLI
    or the approval app, exactly as v1's already does (boundary B4).

    For each target principal (``approvers``, validated as a subset of the
    operation's own snapshot, or the full snapshot when omitted) who has not yet
    decided: get-or-creates their own ``Approval`` row (reused, not re-minted, on a
    second call — the same row's token keeps working across a re-send) and delivers
    one notification, deduplicated by ``(operation_id, principal_id,
    "approval.requested")`` (:func:`_deliver_with_dedup`). A principal who already
    decided is silently skipped from ``notified`` — nothing left to route to them.

    ``message`` is advisory only (ADR-007) — shown alongside the notification,
    never affects policy.
    """
    row = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="request_approval", enable_v2=enable_v2
    )
    if row.state != "PENDING_APPROVAL":
        raise InvalidStateTransitionError(
            details={"current_state": row.state, "requested": "PENDING_APPROVAL"}
        )
    snapshot = _v2_quorum_snapshot(row)
    if snapshot is None:
        raise InvalidStateTransitionError(
            details={"reason": "operation has no v2 approval-policy snapshot"}
        )
    eligible_approvers: list[str] = snapshot["eligible_approvers"]
    quorum_count: int = snapshot["quorum_count"]

    if approvers is not None:
        unknown = sorted(set(approvers) - set(eligible_approvers))
        if unknown:
            raise ApproverNotInPolicyError(details={"unknown_approvers": unknown})
        targets = [p for p in eligible_approvers if p in approvers]
    else:
        targets = eligible_approvers

    already_decided = {
        a.decided_by
        for a in ApprovalRepository(session).list_for_operation(operation_id)
        if a.decision is not None
    }
    notified: list[str] = []
    for target in targets:
        if target in already_decided:
            continue
        _get_or_mint_own_approval_row(session, row, decided_by=target)
        event = NotificationEvent(
            event_type="approval.requested",
            subject_type="operation",
            subject_id=operation_id,
            principal_id=target,
            occurred_at=datetime.now(UTC),
            fetch_reference=f"n8n-operator operations approval-status {operation_id}",
        )
        _deliver_with_dedup(session, sink=sink, event=event)
        notified.append(target)

    audit_writer.write(
        AuditLogRepository(session),
        actor=principal_id,
        action="approval.routed",
        subject_type="operation",
        subject_id=operation_id,
        outcome="allowed",
        detail={"notified": notified, "message": message} if message else {"notified": notified},
    )
    return RequestApprovalResult(
        operation_id=operation_id,
        quorum_count=quorum_count,
        approval_policy_snapshot=eligible_approvers,
        notified=notified,
        state=row.state,
    )


def get_approval_status(
    session: Session, *, operation_id: str, principal_id: str, enable_v2: bool = True
) -> ApprovalStatus:
    """Which approvals have been collected, which are outstanding, against the
    required quorum (MCP_TOOLS.md section 5.4, ADR-017)."""
    row = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="get_approval_status", enable_v2=enable_v2
    )
    snapshot = _v2_quorum_snapshot(row)
    if snapshot is None:
        return ApprovalStatus(
            operation_id=operation_id,
            quorum_count=1,
            approval_policy_snapshot=[],
            decisions=[],
            outstanding=[],
            ready=row.state == "APPROVED",
        )
    context = _approval_decision_context(session, row, approval_row=None)
    return ApprovalStatus(
        operation_id=operation_id,
        quorum_count=context.quorum_count,
        approval_policy_snapshot=snapshot["eligible_approvers"],
        decisions=context.decisions,
        outstanding=context.outstanding_approvers,
        ready=row.state == "APPROVED",
    )


def cancel_operation(
    session: Session,
    *,
    operation_id: str,
    principal_id: str,
    reason: str | None = None,
    enable_v2: bool = False,
) -> Operation:
    """T09/T12: the originating caller withdraws before execution (MCP_TOOLS.md 2.9).

    ``reason`` is advisory only, exactly like ``prepare_operation``'s (ADR-007) — it is
    recorded on the transition's audit detail for a human reading the trail later, and
    never affects whether the cancellation is allowed.
    """
    row = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="cancel_operation", enable_v2=enable_v2
    )
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


def _verify_live_before_execute(preflight: PreflightPort, entry: WorkflowContract) -> None:
    """Re-run preflight immediately before dispatch (ARCHITECTURE.md 4.3 step 4): the
    workflow could have been edited directly in n8n's UI during the approval wait,
    which the registry-snapshot check in :func:`execute_operation` cannot catch on its
    own (the registry's own ``definition_hash`` is author-asserted at load time, not
    derived from a live fetch — only a live re-check can observe a live edit). Reuses
    the exact preflight logic ``prepare_operation`` already ran once, rather than a
    second, parallel drift-detection path — ADR-009 section 6 keeps drift detection in
    exactly one place for the same reason: "there is no second, later verification
    path to get subtly wrong."

    Mirrors ``prepare_operation``'s own use of this port: ``result.ready`` is the fast
    path, exactly as it is there. Only when ``ready`` is ``False`` are the individual
    named checks inspected, to pick the specific typed error MCP_TOOLS.md 2.8's error
    list names — ``InstanceUnreachableError``, ``WorkflowMissingOnInstanceError``,
    ``WorkflowInactiveError``, ``DefinitionDriftError`` — in the same severity order
    the real adapter's own checks cascade in (a failed ``instance_reachable`` leaves
    every check after it ``skipped``, so checking in this order and stopping at the
    first ``fail`` naturally picks the root cause). A check this function does not
    itself name (``compatible_version``, ``trigger_compatibility``,
    ``credential_bindings``, …) failing still blocks execution, conservatively, as
    ``InstanceUnreachableError`` — the most generic "can't proceed with n8n" error
    MCP_TOOLS.md 2.8 offers.
    """
    result = preflight.check(entry)
    if result.ready:
        return

    checks = {c.check: c for c in result.checks}
    reachable = checks.get("instance_reachable")
    if reachable is not None and reachable.status == "fail":
        raise InstanceUnreachableError()
    exists = checks.get("workflow_exists")
    if exists is not None and exists.status == "fail":
        raise WorkflowMissingOnInstanceError()
    active = checks.get("workflow_active")
    if active is not None and active.status == "fail":
        raise WorkflowInactiveError()
    unchanged = checks.get("definition_unchanged")
    if unchanged is not None and unchanged.status == "fail":
        detail = unchanged.detail
        raise DefinitionDriftError(details=detail if isinstance(detail, dict) else {})

    raise InstanceUnreachableError()


def execute_operation(
    session: Session,
    *,
    operation_id: str,
    handle: str,
    principal_id: str,
    preflight: PreflightPort,
    environment: str = "default",
    enable_v2: bool = False,
) -> Operation:
    """T10: burn the handle and move to ``EXECUTING`` (ADR-003, ARCHITECTURE.md 4.3
    steps 0-6). Dispatching to n8n and resolving to T13/T14/T15 is deliberately **not**
    this function's job — see :func:`dispatch_operation`, which calls this function
    for exactly the burn-and-transition step, then dispatches, then calls
    :func:`record_execution_outcome`.

    Every check below runs, in order, before the handle is ever burned — a caller that
    fails any of them leaves the operation exactly as it was, still ``APPROVED``,
    still holding its unburned handle:

    1. ``handle`` must equal ``operation_id`` (ADR-003: the operation ID *is* the
       handle) — a caller passing two different values is a real error
       (``ARGUMENT_MISMATCH``), not one silently ignored by only ever looking at
       ``operation_id``.
    2. The operation is loaded with lazy expiry applied (invariant I9) and its
       principal verified (``_get_owned_operation_row``) — a mismatch on either reads
       as ``OPERATION_NOT_FOUND``, the same "no signal distinguishing X from Y"
       defense used everywhere else an operation is looked up by a caller-supplied ID.
    3. ``environment`` must match the operation's own recorded environment. v1 has
       exactly one (``"default"``), so this can never actually fail today — an
       explicit, verified check standing in for what a real multi-environment v2 would
       need to enforce for real, the same "make the structural invariant explicit"
       reasoning as phase 6's approval-token binding.
    4. The handle must not already be burned (``HANDLE_ALREADY_USED``).
    5. State must be ``APPROVED`` (``APPROVAL_REQUIRED``/``OPERATION_EXPIRED``/
       ``OPERATION_CANCELED``/``HANDLE_INVALID`` as appropriate).
    6. The argument fingerprint is recomputed from the operation's own stored
       (raw, unredacted) arguments and compared against the one recorded at prepare
       time (invariant I5: "the fingerprint checked at execute is the fingerprint
       recorded at prepare"). Structurally unreachable today — nothing ever mutates
       ``arguments`` after creation — verified explicitly anyway, for the same reason
       as the binding check in ``resolve_approval_token``.
    7. The registered ``definition_hash`` is compared against the registry's own
       *current* active snapshot — catches drift from a ``registry reload`` since
       approval, without touching n8n.
    8-10. The live workflow is re-fetched and re-hashed via :func:`_verify_live_before_execute`
       — catches an edit made directly in n8n's UI, which step 7 cannot see.
    11. The handle is burned (a compare-and-set — ``HANDLE_ALREADY_USED`` if a
        concurrent caller already won it), *then* ``max_concurrent`` is checked: this
        workflow's count of other operations already ``EXECUTING`` must be below its
        configured ceiling (``CONCURRENCY_LIMIT_REACHED``).

    Burning the handle before checking concurrency — rather than after, as the count
    alone would suggest — is deliberate: SQLite allows only one writer transaction at a
    time, and the burn's ``UPDATE`` is what makes *this* transaction acquire that write
    lock. Checking the count first would let two threads racing on *different*
    operations both read the same stale count before either commits, both pass, and
    both burn — the same TOCTOU gap a plain read-then-write has no matter how the
    count is queried. Burning first forces every concurrent caller for the same
    workflow through SQLite's single-writer serialization before the count is read, so
    each sees a count that already reflects every operation that burned ahead of it. A
    refused attempt still leaves the operation exactly as it was: raising here aborts
    the whole caller transaction (``storage.session.session_scope`` rolls back on any
    exception), so the burn is never observed to have happened outside this function.
    """
    if handle != operation_id:
        raise ArgumentMismatchError(details={"operation_id": operation_id, "handle": handle})

    row = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="execute_operation", enable_v2=enable_v2
    )
    # v1 only: the operation's own recorded `environment` must match what the caller
    # (still) says it is — v1 has exactly one value ("default"), so this can never
    # actually fail; it stands in for what a real multi-environment check would need
    # to enforce, the same "make the structural invariant explicit" reasoning as the
    # approval-token binding check elsewhere in this module. v2 has no re-supplied
    # `environment` to validate against here — `operation.environment` was already
    # resolved and fixed once, at `prepare_operation` time; nothing this function's
    # caller could pass would add real coverage over that.
    if not enable_v2 and row.environment != environment:
        raise OperationNotFoundError()

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

    recomputed_fingerprint = fingerprint_arguments(canonicalize_arguments(row.arguments))
    if recomputed_fingerprint != row.argument_fingerprint:
        raise ArgumentMismatchError(details={"operation_id": operation_id})

    document = _require_active_document(session)
    current_entry = _find_entry(document, row.workflow_id)
    current_hash = current_entry.definition_hash if current_entry is not None else None
    if current_entry is None or current_hash != row.definition_hash:
        raise DefinitionDriftError(
            details={"registered": row.definition_hash, "current": current_hash}
        )

    _verify_live_before_execute(preflight, current_entry)

    burned = OperationRepository(session).burn_handle(operation_id=operation_id)
    if not burned:
        raise HandleAlreadyUsedError()

    concurrent = OperationRepository(session).count_in_states(
        workflow_id=row.workflow_id, states=["EXECUTING"]
    )
    if concurrent >= current_entry.limits.max_concurrent:
        raise ConcurrencyLimitReachedError(
            details={"max_concurrent": current_entry.limits.max_concurrent, "current": concurrent}
        )

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
    node_trace: dict[str, Any] | None = None,
    known_secrets: Sequence[str] = (),
) -> Operation:
    """T13/T14/T15: record what n8n reported for an already-``EXECUTING`` operation.

    The seam Phase 4's n8n adapter calls after dispatching — this function never talks
    to n8n itself, only records what the caller already determined. ``result`` and
    ``error`` are mutually exclusive; whichever is given is redacted per the workflow's
    ``output.redact``, scrubbed of any string in ``known_secrets`` (boundary B5/B6), and
    size-capped per ``output.max_bytes`` (``core/redaction.py``) before persistence — the
    same shaping ``get_execution_result`` later reads back.

    ``node_trace`` is stored as-is (already allowlist-shaped and payload-free by
    :func:`n8n_operator.n8n.client.N8nClient.get_execution_node_trace` — see that
    method's docstring for why it is safe by construction) rather than passed through
    ``redact``/``cap_output``: it never carries a node's ``data.main`` in the first
    place, so there is nothing in it for the workflow's redaction rules to catch.

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
        node_trace=node_trace,
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


def dispatch_operation(
    session_factory: sessionmaker[Session],
    *,
    operation_id: str,
    principal_id: str,
    dispatch: DispatchPort,
    known_secrets: Sequence[str] = (),
) -> Operation:
    """Dispatch an already-``EXECUTING`` operation to n8n exactly once, then record
    whatever came back (T13/T14/T15).

    The **one** function in this module that manages its own transactions rather than
    taking a caller's open ``Session`` — every other use case here is a pure sequence
    of writes with no I/O in between, so a single caller-managed transaction is the
    right shape (module docstring, invariant I6). This one sandwiches a real HTTP call
    to n8n between two separate transactions, because holding a database transaction
    open across a network call is exactly the kind of thing that turns a slow n8n
    instance into a database outage:

    1. Read the ``EXECUTING`` row's raw (unredacted) arguments and its frozen entry.
    2. Call ``dispatch.dispatch(...)`` and, if warranted, ``dispatch.fetch_node_trace``
       — no session held open for either.
    3. Record the outcome via :func:`record_execution_outcome`, in a fresh transaction.

    A crash between steps 2 and 3 is exactly the "lost response" case ADR-009 already
    requires every caller to tolerate: the operation is left in ``EXECUTING``,
    unresolved, rather than silently reclassified — never retried, never dispatched a
    second time to resolve the ambiguity (ADR-005). Reconciling it is an out-of-band
    operator action, not something this function attempts on its own.

    ``fetch_node_trace`` is called only when the dispatch outcome itself reports a
    trustworthy correlation (``correlation_available``) *and* the workflow's own
    contract opts in (``output.include_node_trace``) — never guessed from a nearby
    execution.
    """
    with session_scope(session_factory) as session:
        row = _get_owned_operation_row(session, operation_id, principal_id)
        if row.state != "EXECUTING":
            raise InvalidStateTransitionError(details={"current_state": row.state})
        entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
        arguments = row.arguments
        timeout_seconds = entry.limits.timeout_seconds or 60
        started_at = row.updated_at  # the T10 timestamp: when EXECUTING began

    outcome = dispatch.dispatch(entry, arguments, timeout_seconds=timeout_seconds)
    finished_at = datetime.now(UTC) if outcome.kind != "indeterminate" else None

    node_trace: dict[str, Any] | None = None
    if (
        outcome.correlation_available
        and outcome.execution_id is not None
        and entry.output.include_node_trace
    ):
        node_trace = dispatch.fetch_node_trace(outcome.execution_id)

    with session_scope(session_factory) as session:
        if outcome.kind == "success":
            result = (
                outcome.result if isinstance(outcome.result, dict) else {"value": outcome.result}
            )
            return record_execution_outcome(
                session,
                operation_id=operation_id,
                outcome="success",
                started_at=started_at,
                finished_at=finished_at,
                n8n_execution_id=outcome.execution_id,
                result=result,
                node_trace=node_trace,
                known_secrets=known_secrets,
            )
        if outcome.kind == "error":
            return record_execution_outcome(
                session,
                operation_id=operation_id,
                outcome="error",
                started_at=started_at,
                finished_at=finished_at,
                n8n_execution_id=outcome.execution_id,
                error={"http_status": outcome.http_status, "body": outcome.result},
                node_trace=node_trace,
                known_secrets=known_secrets,
            )
        return record_execution_outcome(
            session,
            operation_id=operation_id,
            outcome="indeterminate",
            started_at=started_at,
            finished_at=finished_at,
            n8n_execution_id=outcome.execution_id,
            error={"http_status": outcome.http_status} if outcome.http_status is not None else None,
            node_trace=node_trace,
            known_secrets=known_secrets,
        )


def get_operation(
    session: Session, *, operation_id: str, principal_id: str, enable_v2: bool = False
) -> Operation:
    """Current state of one operation, applying any overdue expiry first (invariant I9,
    MCP_TOOLS.md section 2.7).

    Arguments are echoed **post-redaction** (MCP_TOOLS.md 2.7's own example: an email
    address shown as ``"[REDACTED]"``) — the row itself holds the raw values phase 7's
    dispatch and fingerprint re-verification need, so redaction happens here, at the
    read boundary, not at rest.
    """
    row = _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="get_operation", enable_v2=enable_v2
    )
    entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
    operation = _to_domain(row)
    return operation.model_copy(update={"arguments": redact(row.arguments, entry.output.redact)})


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
    enable_v2: bool = False,
) -> list[Operation]:
    """Filterable history (MCP_TOOLS.md section 2.10) — applies lazy expiry to every
    returned row, since a list is a read like any other (invariant I9).

    ``cursor`` is opaque to the caller (MCP_TOOLS.md 2.10: "Opaque pagination cursor")
    but is, concretely, the ``operation_id`` of the last row a previous page returned:
    operation IDs are ULIDs, so ``id`` order and ``created_at`` order agree, and
    "everything strictly older than this ID" is a stable page boundary without a
    separate offset concept. The MCP adapter mints the next page's cursor from the
    last operation in a full page and omits it once a page comes back short.

    v1 (``enable_v2=False``): unchanged — every row belongs to ``principal_id``
    (ownership-scoped), and ``environment`` (still the legacy free-text value, always
    ``"default"``) is an ordinary exact-match filter. v2: visibility is role/scope-
    based, not ownership-based (ADR-015) — an operator/approver/admin whose grants
    cover a workflow sees every principal's operations against it, not only their
    own. ``environment`` is a real environment argument, standard-resolved the same
    way every other v2 tool resolves one (Stage 04, MCP_TOOLS.md section 5.9) —
    omitted with more than one environment visible raises
    :class:`~n8n_operator.errors.EnvironmentRequiredError`, exactly like
    ``describe_workflow``. The scope filter is pushed into the SQL query itself,
    *before* ``LIMIT`` (``OperationRepository.list``'s own
    ``workflow_id_like_patterns``), so a cursor can never walk past a row the filter
    would have hidden (the pagination side channel the completion gate names). A
    caller-supplied ``workflow_id`` filter that the caller isn't authorized for
    resolves to zero rows, not an error — consistent with ``list_workflows``'s own
    "filtering, not authorization" framing for a list endpoint.
    """
    if not (1 <= limit <= 100):
        raise InvalidArgumentsError(details={"limit": limit})
    if states is not None:
        unknown = [s for s in states if s not in STATES]
        if unknown:
            raise InvalidArgumentsError(details={"unknown_states": unknown})

    scoped_principal_id: str | None = principal_id
    like_patterns: list[str] | None = None
    resolved_environment: str | None = environment
    if enable_v2:
        # v2's `environment` argument names a real environment (MCP_TOOLS.md section
        # 5.9), resolved by the same rule every other v2 tool uses — not the legacy
        # v1 free-text filter this parameter doubles as. The resolved id is exactly
        # what `Operation.environment` holds for a v2-prepared row (`prepare_
        # operation`'s own doc), so filtering by it below is still the same simple
        # exact-match column comparison.
        resolved_environment = identity.resolve_environment(
            session, principal_id=principal_id, environment=environment
        ).id
        memberships = OrganizationMembershipRepository(session).list_active_for_principal(
            principal_id
        )
        if workflow_id is not None:
            decision = _authorize(
                session,
                principal_id=principal_id,
                tool_name="list_operations",
                workflow_id=workflow_id,
                enable_v2=enable_v2,
            )
            if not decision.allowed:
                return []
        else:
            patterns: list[str] = []
            for membership in memberships:
                if "list_operations" not in {
                    tool
                    for role in membership.roles
                    for tool in authorization.capabilities_for_role(role)
                }:
                    continue
                if membership.environment_scope != ["*"]:
                    continue
                patterns.append(authorization.workflow_scope_to_sql_like(membership.workflow_scope))
            like_patterns = patterns
        scoped_principal_id = None

    rows = OperationRepository(session).list(
        principal_id=scoped_principal_id,
        environment=resolved_environment,
        workflow_id=workflow_id,
        workflow_id_like_patterns=like_patterns,
        states=states,
        since=since,
        limit=limit,
        before_id=cursor,
    )
    return [_to_domain(_apply_lazy_expiry(session, row)) for row in rows]


def get_execution_result(
    session: Session, *, operation_id: str, principal_id: str, enable_v2: bool = False
) -> ExecutionResult:
    """The redacted, size-capped result of a completed operation (MCP_TOOLS.md 2.11)."""
    _get_owned_operation_row(
        session, operation_id, principal_id, tool_name="get_execution_result", enable_v2=enable_v2
    )
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


def list_environments(session: Session, *, principal_id: str) -> list[EnvironmentSummary]:
    """Every environment visible to ``principal_id`` (MCP_TOOLS.md section 5.9,
    ADR-016 section 4) — v2 only; callers gate registration on ``deps.enable_v2`` the
    same way ``whoami`` does.

    Archived environments are included only for a caller who is ``admin`` in that
    environment's own organization (ADR-016 section 4: an archived environment stays
    resolvable forever by ID, but does not appear in a *list* to anyone else) — never
    an instance URL, a raw workflow ID, or a secret reference, and never an archived
    environment's name to a non-admin (it is simply omitted, the same "absent, not
    denied" shape ``list_workflows`` already uses for filtering).

    ``approval_policy_summary`` is computed from this environment's own resolved
    (base + overlay) workflow contracts — never a raw policy dump — so a caller sees
    "3 of 5 workflows require approval" for an environment whose overlays tighten
    approval, and the base registry's own count for one that has none.
    """
    memberships = OrganizationMembershipRepository(session).list_active_for_principal(principal_id)
    admin_org_ids = {m.organization_id for m in memberships if "admin" in m.roles}
    visible = identity.list_visible_environments(
        session, principal_id=principal_id, include_archived=True
    )
    document = _require_active_document(session)
    overlays = WorkflowEnvironmentOverlayRepository(session)
    summaries: list[EnvironmentSummary] = []
    for env in visible:
        archived = env.archived_at is not None
        if archived and env.organization_id not in admin_org_ids:
            continue
        enabled_entries = [entry for entry in document.workflows if entry.enabled]
        requires_approval = 0
        for entry in enabled_entries:
            overlay_row = overlays.get(entry.id, env.id)
            overlay_entry = (
                _overlay_entry_from_row(overlay_row, workflow_id=entry.id)
                if overlay_row is not None
                else None
            )
            merged = resolve_overlay(entry, overlay_entry)
            if merged.approval == "required":
                requires_approval += 1
        summaries.append(
            EnvironmentSummary(
                environment_id=env.id,
                organization_id=env.organization_id,
                name=env.name,
                is_production=env.is_production,
                archived=archived,
                approval_policy_summary=(
                    f"{requires_approval} of {len(enabled_entries)} workflows require approval"
                ),
            )
        )
    return summaries


# ----------------------------------------------------------------------------------
# Audit (phase 8, BUILD_PLAN section 9.4). Operator-level views across every
# principal — unlike the read paths above, nothing here is scoped to a caller.
# ----------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _require_admin(session: Session, *, principal_id: str | None, enable_v2: bool) -> None:
    """Gate for the CLI's system-wide, cross-principal administrative reads
    (``audit verify``/``audit export``, Stage 03) — not workflow-scoped, so
    :func:`_authorize`'s ``WorkflowNotFoundError``/``OperationNotFoundError`` shapes
    don't apply; there is no object being enumerated here for invariant I14 to protect,
    only a capability being checked (``core/authorization.py``'s ``has_role``)."""
    if not enable_v2:
        return
    assert principal_id is not None  # every v2 caller is authenticated
    memberships = OrganizationMembershipRepository(session).list_active_for_principal(principal_id)
    if not authorization.has_role(memberships, "admin"):
        raise InsufficientRoleError()


def verify_audit_chain(
    session: Session, *, principal_id: str | None = None, enable_v2: bool = False
) -> ChainVerificationResult:
    """AC-22: walk the full ``audit_log`` table, in ``seq`` order, and report the first
    break, if any — ``n8n-operator audit verify``'s entire read path. A clean database
    reports ``ok=True``; a single row mutated in place is caught at its exact sequence
    number (BUILD_PLAN section 9.4: tamper-*evidence*, not tamper-*proofing*)."""
    _require_admin(session, principal_id=principal_id, enable_v2=enable_v2)
    entries = AuditLogRepository(session).list_all()
    return verify_chain(entries)


def export_audit_record(
    session: Session,
    *,
    known_secrets: Sequence[str] = (),
    principal_id: str | None = None,
    enable_v2: bool = False,
) -> dict[str, Any]:
    """AC-25: a complete, chain-verifiable, redacted export of every operation and the
    full audit log — everything a separate process needs to independently re-verify
    the hash chain and inspect what happened, without ever including a credential, a
    webhook secret, raw unredacted arguments or results, or an approval token.

    Arguments are redacted here, at the export boundary, exactly like
    :func:`get_operation`: ``operations.arguments`` has been stored raw at rest since
    phase 7 (dispatch and fingerprint re-verification need the real values), so nothing
    upstream of a read boundary is safe to hand out unredacted. ``known_secrets``, when
    given, additionally scrubs by *value* (the operator's configured n8n API key or
    webhook secret, wherever it might appear) — the same defense-in-depth
    :func:`record_execution_outcome` already applies to results. ``n8n-operator audit
    export`` itself does not require n8n configuration to run (like every other
    ``operations``/``audit`` command) and so calls this with no secrets to scrub,
    relying on the structural guarantee that a credential is never written to the
    database in the first place (ADR-006) — a caller with n8n configuration loaded may
    pass its known secrets for an extra, cheap layer of defense.

    ``execution_results.redacted_payload``/``error`` are already redacted and
    size-capped at write time (:func:`record_execution_outcome`) — exported as stored.
    ``execution_results.node_trace`` is allowlist-shaped by construction
    (``n8n/client.py::get_execution_node_trace``) and never carries a raw payload.

    Approval tokens are never exported: only their *hash* is ever stored
    (``approvals.token_hash``), and this export does not include the ``approvals``
    table at all — ``operation_events`` already carries the T06/T07 decision, actor,
    and timestamp verification needs, without a reason to touch that table.
    """
    _require_admin(session, principal_id=principal_id, enable_v2=enable_v2)
    audit_entries = AuditLogRepository(session).list_all()
    chain_result = verify_chain(audit_entries)
    audit_log = [
        {
            "seq": entry.seq,
            "prev_hash": entry.prev_hash,
            "entry_hash": entry.entry_hash,
            "occurred_at": _iso(entry.occurred_at),
            "actor": entry.actor,
            "action": entry.action,
            "subject_type": entry.subject_type,
            "subject_id": entry.subject_id,
            "outcome": entry.outcome,
            "detail": entry.detail,
        }
        for entry in audit_entries
    ]

    event_repo = OperationEventRepository(session)
    result_repo = ExecutionResultRepository(session)
    operations: list[dict[str, Any]] = []
    referenced_snapshot_ids: set[str] = set()
    for row in OperationRepository(session).list_all():
        entry = _entry_for_operation(session, row.snapshot_id, row.workflow_id)
        arguments = scrub_secrets(redact(row.arguments, entry.output.redact), known_secrets)
        events = [
            {
                "from_state": event.from_state,
                "to_state": event.to_state,
                "transition": event.transition,
                "actor": event.actor,
                "detail": event.detail,
                "occurred_at": _iso(event.occurred_at),
            }
            for event in event_repo.list_for_operation(row.id)
        ]
        result_row = result_repo.get(row.id)
        execution_result = (
            None
            if result_row is None
            else {
                "status": result_row.status,
                "n8n_execution_id": result_row.n8n_execution_id,
                "started_at": _iso(result_row.started_at),
                "finished_at": _iso(result_row.finished_at),
                "redacted_payload": result_row.redacted_payload,
                "node_trace": result_row.node_trace,
                "error": result_row.error,
            }
        )
        referenced_snapshot_ids.add(row.snapshot_id)
        operations.append(
            {
                "id": row.id,
                "principal_id": row.principal_id,
                "environment": row.environment,
                "workflow_id": row.workflow_id,
                "snapshot_id": row.snapshot_id,
                "definition_hash": row.definition_hash,
                "state": row.state,
                "arguments": arguments,
                "argument_fingerprint": row.argument_fingerprint,
                "argument_bytes": row.argument_bytes,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "events": events,
                "execution_result": execution_result,
            }
        )

    snapshot_repo = RegistrySnapshotRepository(session)
    registry_snapshots: list[dict[str, Any]] = []
    for snapshot_id in sorted(referenced_snapshot_ids):
        snapshot = snapshot_repo.get(snapshot_id)
        if snapshot is None:
            continue  # snapshots are never deleted; defensive only
        registry_snapshots.append(
            {
                "id": snapshot.id,
                "content_hash": snapshot.content_hash,
                "source_path": snapshot.source_path,
                "document": snapshot.document,
                "loaded_at": _iso(snapshot.loaded_at),
            }
        )

    return {
        "exported_at": _iso(datetime.now(UTC)),
        "chain": {
            "ok": chain_result.ok,
            "first_break_seq": chain_result.first_break_seq,
            "reason": chain_result.reason,
            "entry_count": len(audit_entries),
        },
        "audit_log": audit_log,
        "operations": operations,
        "registry_snapshots": registry_snapshots,
    }
