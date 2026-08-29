"""Optional agent-audit emission alongside n8n-operator's own audit log.

n8n-operator already has the approval semantics this project's own
BUILD_PLAN and ADRs describe in detail — a twelve-state operation
lifecycle, a hash-chained ``audit_log``, invariant I6 (one audit row per
transition, same transaction). What it doesn't have is a *portable*
record of that lifecycle: ``audit_log`` is n8n-operator's own schema, not
something another team's observability backend can ingest without
writing an n8n-operator-specific adapter.

This module is a thin, best-effort bridge from n8n-operator's own
transition vocabulary onto https://github.com/katekruger/agent-audit's
three-phase event model (``proposed`` / ``decided`` / ``executed``),
emitted as OpenTelemetry LogRecord Events via ``agent_audit_record``.

**Fully optional, in every direction:**

- If ``agent_audit_record`` is not installed, every function here is a
  no-op — n8n-operator's own audit log is unaffected either way.
- If it *is* installed but misconfigured (no OTel exporter, a
  misbehaving one), ``agent_audit_record.Emitter`` itself never raises —
  see its own "never crash the host" guarantee. The one thing this
  module additionally guards against is a mapping bug in *this* file
  (e.g. a decision that violates agent-audit's own schema invariants);
  those are caught and logged here too, so a bug in this bridge can never
  take down an n8n-operator transition.
- Nothing here participates in ``core/service.py``'s transaction. A
    failure or slowness here must never affect whether an operation
    transition commits — see ``docs/integrations/agent-audit.md``.

See ``docs/integrations/agent-audit.md`` for the full transition-to-event
mapping table and the reasoning behind each row, including the two
transitions (T02 UNKNOWN's T15, and the PREPARING-phase denials T02/T03)
where the mapping is a judgment call rather than an obvious 1:1 fit.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_audit_record import Decision, NotExecutedReason, Outcome, PrincipalType

_LOG = logging.getLogger(__name__)

try:
    from agent_audit_record import (
        ActorType,
        Cost,
        Decision,
        Emitter,
        NotExecutedReason,
        Outcome,
        PrincipalType,
    )

    _EMITTER: Emitter | None = Emitter()

    # Transitions that resolve PREPARING/PENDING_APPROVAL into a decision.
    # T01 (creation), T04 (PREPARING->PENDING_APPROVAL), and T10
    # (APPROVED->EXECUTING) are intentionally absent: T01 is `proposed`
    # (handled by emit_proposed, not this table); T04 and T10 are
    # intermediate states with no agent-audit event of their own.
    _TRANSITION_DECIDED: dict[str, tuple[Decision, PrincipalType]] = {
        "T02": (Decision.AUTO_DENY, PrincipalType.POLICY),  # invalid input; no approval sought
        "T03": (Decision.DENY, PrincipalType.POLICY),  # blocked by policy before approval
        "T05": (Decision.AUTO_ALLOW, PrincipalType.POLICY),
        "T06": (Decision.ALLOW, PrincipalType.HUMAN),
        "T07": (Decision.DENY, PrincipalType.HUMAN),
        "T08": (Decision.TIMEOUT, PrincipalType.TIMEOUT),
        "T09": (Decision.CANCEL, PrincipalType.HUMAN),
    }

    # Transitions that resolve an already-decided operation to a final
    # execution outcome.
    _TRANSITION_EXECUTED: dict[str, tuple[Outcome, NotExecutedReason | None]] = {
        "T11": (Outcome.NOT_EXECUTED, NotExecutedReason.EXPIRED),
        "T12": (Outcome.NOT_EXECUTED, NotExecutedReason.CANCELLED),
        "T13": (Outcome.SUCCESS, None),
        "T14": (Outcome.FAILURE, None),
        # T15 (APPROVED/EXECUTING -> UNKNOWN): agent-audit's Outcome enum
        # has no "indeterminate" value. FAILURE is the closer of the two
        # options (vs. success) -- see docs/integrations/agent-audit.md.
        "T15": (Outcome.FAILURE, None),
    }
except ImportError:
    # agent_audit_record is not installed (the default -- see the
    # 'agent-audit' dependency group). Every public function below checks
    # `_EMITTER is None` before touching either table, so empty tables
    # here are never consulted; they only need to exist so this module
    # itself always imports cleanly, regardless of whether the optional
    # dependency is present. A NameError here would take down every
    # module that imports core.service -- i.e. the whole application --
    # over a dependency this integration is supposed to be optional to.
    _EMITTER = None
    _TRANSITION_DECIDED = {}
    _TRANSITION_EXECUTED = {}


def emit_proposed(
    *, operation_id: str, principal_id: str, environment: str, workflow_id: str
) -> None:
    """Call once, at T01 (operation creation) -- alongside, never instead
    of, the existing ``audit_writer.write`` call."""
    if _EMITTER is None:
        return
    try:
        _EMITTER.proposed(
            action_id=operation_id,
            actor_id=principal_id,
            actor_type=ActorType.AGENT,
            target_system=f"n8n:{environment}",
            target_resource=workflow_id,
            target_operation="execute_workflow",
        )
    except Exception:
        _LOG.warning(
            "agent-audit: failed to emit proposed for operation %s", operation_id, exc_info=True
        )


def emit_transition(transition_id: str, *, operation_id: str, actor: str) -> None:
    """Call for every transition applied through ``_apply_and_audit`` --
    alongside, never instead of, the existing ``audit_writer.write`` call.
    A no-op for T04/T10, which have no agent-audit event of their own.
    """
    if _EMITTER is None:
        return
    try:
        if transition_id in _TRANSITION_DECIDED:
            decision, principal_type = _TRANSITION_DECIDED[transition_id]
            cost = Cost(wasted=True) if decision.forbids_execution else None
            _EMITTER.decided(
                action_id=operation_id,
                decision=decision,
                principal_type=principal_type,
                principal_id=actor,
                cost=cost,
            )
        elif transition_id in _TRANSITION_EXECUTED:
            outcome, not_executed_reason = _TRANSITION_EXECUTED[transition_id]
            _EMITTER.executed(
                action_id=operation_id, outcome=outcome, not_executed_reason=not_executed_reason
            )
    except Exception:
        _LOG.warning(
            "agent-audit: failed to emit %s for operation %s",
            transition_id,
            operation_id,
            exc_info=True,
        )


def emit_prepare_denied(
    *, workflow_id: str, principal_id: str, environment: str, reason: str
) -> None:
    """T-less: a prepare attempt refused before any operation row exists
    (oversized arguments, rate limit) -- ADR-011's "the attempt is still
    audited" applies here too. Mints its own synthetic action_id since
    there is no operation id to correlate against.
    """
    if _EMITTER is None:
        return
    action_id = f"prepare-denied-{uuid.uuid4()}"
    try:
        _EMITTER.proposed(
            action_id=action_id,
            actor_id=principal_id,
            actor_type=ActorType.AGENT,
            target_system=f"n8n:{environment}",
            target_resource=workflow_id,
            target_operation="execute_workflow",
        )
        _EMITTER.decided(
            action_id=action_id,
            decision=Decision.DENY,
            principal_type=PrincipalType.POLICY,
            reason=reason,
            cost=Cost(wasted=True),
        )
    except Exception:
        _LOG.warning(
            "agent-audit: failed to emit prepare-denied for workflow %s", workflow_id, exc_info=True
        )
