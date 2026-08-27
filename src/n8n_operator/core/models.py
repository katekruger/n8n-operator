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
    "AuditEvent",
    "Environment",
    "ExecutionResult",
    "HealthCheckResult",
    "Operation",
    "OperationEvent",
    "PreflightCheck",
    "PreflightResult",
    "Principal",
    "WorkflowContract",
]


class Environment(StrEnum):
    """v1 has exactly one environment (BUILD_PLAN section 8.1: "Explicit in v1, where
    it is always ``default``"). Modeled as an enum, not a bare string, so v2's additional
    environments extend this type rather than validate against nothing at all."""

    DEFAULT = "default"


class Principal(BaseModel):
    """Who acted (BUILD_PLAN section 8.1). v1 holds exactly one row, ``kind="local"``."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: Literal["local", "user", "service"]
    display_name: str
    external_subject: str | None = None
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
