"""Domain types: Operation, Principal, ExecutionResult, and friends.

Pydantic v2 models shared by every adapter, so validation and schema generation are
identical across transports (ADR-001). Every ``core/service.py`` use case returns one of
these — never a live SQLAlchemy row — so a caller never needs a session to read a result
and never observes a value that becomes stale the moment the session closes.

``WorkflowContract`` is the resolved registry entry a workflow's contract is: the same
:class:`~n8n_operator.registry.schema.WorkflowEntry` Phase 2 already defines, re-exported
under the domain-facing name this phase's task list uses. Defining a second, parallel model
here would just be the same fields duplicated with a chance to drift from the one the
registry loader actually produces.

"Structured errors" (also listed among this phase's domain models) are the
:class:`~n8n_operator.errors.OperatorError` hierarchy Phase 1 already implements in full —
nothing new is added here; see ``errors.py``.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from n8n_operator.registry.schema import WorkflowEntry as WorkflowContract

__all__ = [
    "Approval",
    "ApprovalDecisionContext",
    "ApprovalDecisionEntry",
    "ApprovalStatus",
    "AuditEvent",
    "DeliveryOutcome",
    "DeliveryReceipt",
    "DispatchOutcome",
    "Environment",
    "EnvironmentSummary",
    "ExecutionLookup",
    "ExecutionResult",
    "HealthCheckResult",
    "NotificationEvent",
    "Operation",
    "OperationEvent",
    "PreflightCheck",
    "PreflightResult",
    "Principal",
    "ReconciliationRecord",
    "RequestApprovalResult",
    "WorkflowContract",
]


class Environment(StrEnum):
    """v1 has exactly one environment (BUILD_PLAN section 8.1: "Explicit in v1, where
    it is always ``default``"). Modeled as an enum, not a bare string, so v2's additional
    environments extend this type rather than validate against nothing at all."""

    DEFAULT = "default"


class EnvironmentSummary(BaseModel):
    """One row of ``list_environments`` (MCP_TOOLS.md section 5.9, stage 04) — never
    an instance URL, a raw workflow ID, a secret reference, or a hidden (archived, to
    a non-admin) environment's name (ADR-016 section 4)."""

    model_config = ConfigDict(frozen=True)

    environment_id: str
    organization_id: str
    name: str
    is_production: bool
    archived: bool
    approval_policy_summary: str


class Principal(BaseModel):
    """Who acted (BUILD_PLAN section 8.1). v1 holds exactly one row, ``kind="local"``.

    ``external_issuer``, ``disabled_at``, and ``credential_ref`` are v2 (ADR-013,
    ADR-014; stage 02)."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: Literal["local", "user", "service"]
    display_name: str
    external_subject: str | None = None
    external_issuer: str | None = None
    disabled_at: datetime | None = None
    credential_ref: str | None = None
    created_at: datetime


class Operation(BaseModel):
    """The governance record (BUILD_PLAN section 8.1) — the unit section 5 describes.

    A detached snapshot of an ``operations`` row at the moment a use case returned it,
    not a live handle to the database — the state it reports is stale the instant a
    concurrent transition lands, exactly as any other read would be.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    principal_id: str
    environment: str
    snapshot_id: str
    workflow_id: str
    definition_hash: str
    state: str
    state_version: int
    arguments: dict[str, Any]
    argument_fingerprint: str
    argument_bytes: int
    idempotency_key: str | None
    handle_burned_at: datetime | None
    approval_expires_at: datetime | None
    execution_deadline: datetime | None
    n8n_execution_id: str | None
    parent_operation_id: str | None
    created_at: datetime
    updated_at: datetime


class OperationEvent(BaseModel):
    """One row of the append-only transition log (BUILD_PLAN section 8.1)."""

    model_config = ConfigDict(frozen=True)

    id: str
    operation_id: str
    from_state: str | None
    to_state: str
    transition: str
    actor: str
    detail: dict[str, Any]
    occurred_at: datetime


class Approval(BaseModel):
    """One out-of-band human decision (BUILD_PLAN section 8.1).

    Deliberately has no ``token``/``token_hash`` field: the raw token is a bearer secret
    that exists only long enough to be hashed before storage, and the hash itself has no
    business leaving ``storage/`` — nothing in the domain layer needs to see it, only
    ``core/handles.py`` (which mints it) and a future approval-channel adapter (which
    verifies a caller-supplied token against ``ApprovalRepository.get_by_token_hash``).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    operation_id: str
    issued_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    client_fingerprint: str | None


class ApprovalDecisionEntry(BaseModel):
    """One eligible approver's decision (or lack of one) — stage 05, ADR-017. The
    per-principal element both ``ApprovalDecisionContext.decisions`` and
    ``ApprovalStatus.decisions`` are built from."""

    model_config = ConfigDict(frozen=True)

    principal_id: str
    decision: Literal["approved", "rejected"]
    decided_at: datetime


class ApprovalDecisionContext(BaseModel):
    """Everything a human needs to make an approve/reject decision, or to check one
    already made — the one shape both approval channels render from (ADR-010: "the CLI
    must now render arguments, risk, side-effect class, and drift status well enough to
    support a real decision"), so the CLI's ``operations approve``/``reject``/
    ``approval-status`` and the web page's ``GET /approve/{token}`` can never disagree
    about what a pending operation looked like when it was decided.

    Workflow fields (``title``, ``description``, ``risk``, ``side_effects``) are read
    from the operation's own frozen registry snapshot, not the current one — exactly
    what was true when this operation was offered for approval, even if the registry
    has since been reloaded. ``current_definition_hash`` and ``drifted`` compare that
    frozen hash against the *current* active snapshot's hash for the same workflow ID;
    ``current_definition_hash`` is ``None`` when the workflow is no longer registered
    or enabled at all, which is itself reported as drift.

    ``decision``/``decided_by``/``decided_at``/``decided`` (v1, unchanged) reflect the
    operation's one shared decision — or, in v2 quorum mode, the specific decider's
    own row when this context was resolved via *their* token
    (``resolve_approval_token``) or their own CLI call. ``quorum_count``/``decisions``/
    ``outstanding_approvers`` (v2 only, ``quorum_count`` defaults to 1) are the
    operation-wide tally: every decision cast so far, and who in the snapshot has not
    yet decided. ``assigned_to`` is which principal a *pending* per-approver token
    decides as (``None`` for a v1 shared token).
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str
    workflow_id: str
    title: str
    description: str
    risk: Literal["low", "medium", "high"]
    side_effects: Literal["read_only", "external_write", "irreversible"]
    state: str
    arguments: dict[str, Any]
    registered_definition_hash: str
    current_definition_hash: str | None
    drifted: bool
    created_at: datetime
    approval_expires_at: datetime | None
    execution_deadline: datetime | None
    approval_required: bool
    decided: bool
    decision: Literal["approved", "rejected"] | None
    decided_at: datetime | None
    decided_by: str | None
    assigned_to: str | None = None
    quorum_count: int = 1
    decisions: list[ApprovalDecisionEntry] = []
    outstanding_approvers: list[str] = []
    parent_operation_id: str | None = None


class RequestApprovalResult(BaseModel):
    """``request_approval``'s result (MCP_TOOLS.md section 5.3, stage 05)."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    quorum_count: int
    approval_policy_snapshot: list[str]
    notified: list[str]
    state: str


class ApprovalStatus(BaseModel):
    """``get_approval_status``'s result (MCP_TOOLS.md section 5.4, stage 05)."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    quorum_count: int
    approval_policy_snapshot: list[str]
    decisions: list[ApprovalDecisionEntry]
    outstanding: list[str]
    ready: bool


class ExecutionResult(BaseModel):
    """What n8n returned, post-redaction (BUILD_PLAN section 8.1)."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    n8n_execution_id: str | None
    status: Literal["success", "error", "indeterminate"]
    started_at: datetime | None
    finished_at: datetime | None
    redacted_payload: dict[str, Any]
    node_trace: dict[str, Any] | None
    error: dict[str, Any] | None


class AuditEvent(BaseModel):
    """One append-only, hash-chained audit record (BUILD_PLAN section 9.4)."""

    model_config = ConfigDict(frozen=True)

    seq: int
    prev_hash: str
    entry_hash: str
    occurred_at: datetime
    actor: str
    action: str
    subject_type: str
    subject_id: str
    outcome: Literal["allowed", "denied", "error"]
    detail: dict[str, Any]


class PreflightCheck(BaseModel):
    """One row of a preflight result (MCP_TOOLS.md section 2.5).

    ``status`` is one of ``pass``, ``fail``, ``skipped``, or the two non-blocking
    statuses ADR-009 introduces: ``warn`` (a real capability limitation) and
    ``unverifiable`` (a condition Operator has no supported mechanism to test). Only
    ``fail`` sets :attr:`PreflightResult.ready` to ``False``.
    """

    model_config = ConfigDict(frozen=True)

    check: str
    status: Literal["pass", "fail", "skipped", "warn", "unverifiable"]
    code: str | None = None
    detail: Any | None = None


class PreflightResult(BaseModel):
    """The result of checking that a workflow could run right now (MCP_TOOLS.md 2.5)."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    checks: list[PreflightCheck]
    checked_at: datetime


class HealthCheckResult(BaseModel):
    """Whether the configured n8n instance is reachable (MCP_TOOLS.md section 2.3).

    Carries no URL and no credential — ``get_instance_health`` is a discovery tool, not
    a way to learn where the instance lives (ADR-006). ``n8n_version`` is best-effort:
    n8n exposes no endpoint that returns its own release version
    (docs/N8N_COMPATIBILITY.md section 10), so this is the n8n Public API's own spec
    version when one could be determined — a coarse proxy, not a release number — and
    ``None`` otherwise. ``reason`` is a taxonomy code (never a raw connection error
    string, which could carry the host) and is only set when ``reachable`` is ``False``.
    """

    model_config = ConfigDict(frozen=True)

    reachable: bool
    n8n_version: str | None = None
    latency_ms: int | None = None
    reason: str | None = None
    checked_at: datetime


class DispatchOutcome(BaseModel):
    """The result of one webhook dispatch attempt (ADR-005, ADR-009), converted from
    ``n8n.client.DispatchOutcome`` by the composition root — the same real-type-behind-
    a-port pattern ``PreflightResult``/``HealthCheckResult`` already establish.

    ``result`` is already unwrapped (the envelope's own ``data`` field, when a
    well-formed envelope was found) — see ``n8n/client.py``'s ``DispatchOutcome``
    docstring for exactly what "well-formed" requires and why a malformed one still
    counts as ``success``/``error``, never demotes to ``indeterminate``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["success", "error", "indeterminate"]
    http_status: int | None
    result: Any | None
    execution_id: str | None
    correlation_available: bool


class NotificationEvent(BaseModel):
    """One event to deliver via ``core.service.NotificationSink`` — approval routing
    and (stage 08) alert hooks alike (ADR-018). Carries only what section 4 permits:
    never operation arguments, a workflow's title/description, or an execution
    result — ``fetch_reference`` is a pointer (a CLI command, in v2) to the real
    detail through an *authenticated* channel, not the detail itself."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    subject_type: str
    subject_id: str
    principal_id: str | None
    occurred_at: datetime
    fetch_reference: str


class DeliveryOutcome(BaseModel):
    """The result of exactly *one* ``NotificationSink.deliver`` attempt — all a sink
    itself can possibly know, since dedup/attempt-count/status bookkeeping is
    ``core.service._deliver_with_dedup``'s own concern, computed *after* calling the
    sink, never something the sink is asked to track or report back."""

    model_config = ConfigDict(frozen=True)

    delivered: bool
    detail: str | None = None


class DeliveryReceipt(BaseModel):
    """The result of one ``core.service._deliver_with_dedup`` call — either a real
    delivery attempt (wrapping the sink's own :class:`DeliveryOutcome`) or a dedup
    lookup that never called the sink at all (ADR-018 section 2)."""

    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    delivered: bool
    attempts: int
    status: Literal["delivered", "pending", "failed"]
    detail: str | None = None


class ExecutionLookup(BaseModel):
    """What ``core.service.ReconciliationPort.get_execution`` returns — the exact,
    narrow shape ``reconcile_operation`` needs to verify exact-ID reconciliation
    evidence (ADR-009), converted from ``n8n.types.ExecutionSummary`` by the
    composition root (stage 06's own instance of the ``_PreflightAdapter``/
    ``_HealthAdapter``/``_DispatchAdapter`` pattern — ``core/`` never imports an
    ``n8n/`` type directly). Never the full n8n execution detail — no ``runData``,
    no per-node anything; just enough to confirm *which* n8n workflow actually ran and
    what its outcome was."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    n8n_workflow_id: str
    status: str


class ReconciliationRecord(BaseModel):
    """One recorded reconciliation annotation (stage 06, ADR-009/ADR-012) — an
    ``audit_log`` entry, never a state transition; ``UNKNOWN`` keeps no outgoing edge
    (invariant I7). Echoed back by ``reconcile_operation`` and listed by
    ``list_reconciliation_events``."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    execution_id: str
    n8n_workflow_id: str
    n8n_execution_status: str
    note: str
    actor: str
    recorded_at: datetime
