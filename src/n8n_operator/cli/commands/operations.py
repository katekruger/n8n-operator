"""``n8n-operator operations`` — list, show, cancel, approve, reject, expire,
approval-status.

``approve`` and ``reject`` are the **canonical** v1 approval channel (ADR-010); the
loopback approval page is a convenience alternative over the same core use case. Both
render the workflow's title, risk, side-effect class, redacted arguments, drift status,
and deadlines before asking for confirmation — ADR-010's own consequence of making the
CLI canonical: "the CLI must now render arguments, risk, side-effect class, and drift
status well enough to support a real decision."

``expire`` applies all overdue T08/T11 transitions on demand, for deployments that run no
approval app. It is a maintenance convenience only: lazy transactional expiry is
authoritative, so no expired operation is ever executable regardless of whether this has
run (invariant I9).

``list``/``show`` are read-only history/detail views; ``cancel`` withdraws a
``PENDING_APPROVAL`` or ``APPROVED`` operation before it runs, the same confirm-then-act
shape ``approve``/``reject`` already use. Every command that prints a machine-readable
form takes ``--json`` — sorted keys, so the same underlying state always prints the same
bytes (no reliance on dict insertion order or any other incidental ordering).

None of these commands requires ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY``
to be set — approving, rejecting, and expiring are pure Operator-state operations that
never call n8n, so the operator can act on them even while n8n itself is unreachable
(the same "schema management is orthogonal" reasoning ``db.py``/``registry.py`` already
document, applied here to "governance state management").

Phases 6 and 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import (
    load_settings,
    resolve_database_url,
    resolve_notification_sink_config,
    resolve_secret_reference,
    resolve_v2_identity_flags,
)
from n8n_operator.core import service
from n8n_operator.core.identity import resolve_cli_principal_id
from n8n_operator.core.models import (
    ApprovalDecisionContext,
    DeliveryOutcome,
    ExecutionLookup,
    NotificationEvent,
    Operation,
    ReconciliationRecord,
)
from n8n_operator.errors import (
    ApproverNotInPolicyError,
    InvalidArgumentsError,
    InvalidStateTransitionError,
    OperationNotFoundError,
    ReconciliationNotApplicableError,
)
from n8n_operator.logging_setup import register_secret
from n8n_operator.n8n.client import N8nClient
from n8n_operator.notifications.local import LocalNotificationSink
from n8n_operator.notifications.webhook import WebhookNotificationSink
from n8n_operator.storage.repository import EnvironmentRepository
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)


class _CliNotificationSinkAdapter:
    """Converts a ``notifications/`` package sink's local ``DeliveryOutcome`` into
    ``core.models.DeliveryOutcome`` — the same real-type-behind-a-port conversion
    ``mcp/server.py``'s own ``_NotificationSinkAdapter`` performs, duplicated here in
    miniature rather than imported (this command is its own composition root, like
    ``cli/commands/health.py``'s ``_CliHealthAdapter``)."""

    def __init__(self, impl: LocalNotificationSink | WebhookNotificationSink) -> None:
        self._impl = impl

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        raw = self._impl.deliver(event)
        return DeliveryOutcome(delivered=raw.delivered, detail=raw.detail)


def _cli_notification_sink() -> _CliNotificationSinkAdapter:
    sink, url, token = resolve_notification_sink_config()
    if sink == "webhook":
        assert url is not None and token is not None
        return _CliNotificationSinkAdapter(WebhookNotificationSink(url=url, bearer_token=token))
    return _CliNotificationSinkAdapter(LocalNotificationSink())


app = typer.Typer(
    help="List, inspect, cancel, approve, reject, and expire operations.", no_args_is_help=True
)


def _resolve_principal(session: Session) -> tuple[str, bool]:
    """``(principal_id, enable_v2)`` for the current invocation — v1 (the default)
    resolves the same fixed ``"local"`` identity every command already hardcoded
    (BUILD_PLAN section 8.1); v2 resolves the CLI's own identity (Stage 03,
    ``core.identity.resolve_cli_principal_id``)."""
    enable_v2, dev_principal_id = resolve_v2_identity_flags()
    principal_id = resolve_cli_principal_id(
        session, enable_v2=enable_v2, dev_principal_id=dev_principal_id
    )
    return principal_id, enable_v2


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    """A session factory for the command's lifetime, reporting an uninitialized
    database the same friendly way ``registry reload`` does rather than a raw
    traceback."""
    engine: Engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _operation_not_found_or_exit(operation_id: str) -> None:
    typer.secho(f"No such operation: {operation_id}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _render_context(context: ApprovalDecisionContext) -> None:
    typer.echo(f"operation_id:        {context.operation_id}")
    typer.echo(f"workflow_id:         {context.workflow_id}")
    typer.echo(f"title:               {context.title}")
    typer.echo(f"description:         {context.description}")
    typer.echo(f"risk:                {context.risk}")
    typer.echo(f"side_effects:        {context.side_effects}")
    typer.echo(f"state:               {context.state}")
    typer.echo(f"arguments:           {json.dumps(context.arguments, indent=2, sort_keys=True)}")
    if context.drifted:
        typer.secho(
            "DEFINITION DRIFT — the live workflow no longer matches what was approved:",
            fg=typer.colors.RED,
        )
        typer.secho(f"  registered: {context.registered_definition_hash}", fg=typer.colors.RED)
        typer.secho(
            f"  current:    {context.current_definition_hash or '(workflow no longer registered)'}",
            fg=typer.colors.RED,
        )
        typer.echo(
            f"  Run `n8n-operator registry diff-live {context.workflow_id}` to see what changed."
        )
    else:
        typer.echo(f"definition_hash:     {context.registered_definition_hash} (unchanged)")
    typer.echo(f"created_at:          {context.created_at.isoformat()}")
    if context.parent_operation_id is not None:
        typer.echo(f"retry of:            {context.parent_operation_id}")
    if context.approval_expires_at is not None:
        typer.echo(f"approval_expires_at: {context.approval_expires_at.isoformat()}")
    if context.execution_deadline is not None:
        typer.echo(f"execution_deadline:  {context.execution_deadline.isoformat()}")
    if not context.approval_required:
        typer.echo("approval:            not required (read_only + approval: none)")
    elif context.quorum_count > 1:
        decided_count = len(context.decisions)
        typer.echo(f"approval:            {decided_count} of {context.quorum_count} decided")
        for entry in context.decisions:
            typer.echo(
                f"  {entry.decision:<8} by {entry.principal_id} at {entry.decided_at.isoformat()}"
            )
        for principal_id in context.outstanding_approvers:
            typer.echo(f"  outstanding: {principal_id}")
    elif context.decided:
        typer.echo(
            f"approval:            {context.decision} by {context.decided_by} "
            f"at {context.decided_at.isoformat() if context.decided_at else '?'}"
        )
    else:
        typer.echo("approval:            pending")


def _summary_dict(operation: Operation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "workflow_id": operation.workflow_id,
        "state": operation.state,
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
        "parent_operation_id": operation.parent_operation_id,
    }


def _detail_dict(operation: Operation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "workflow_id": operation.workflow_id,
        "environment": operation.environment,
        "state": operation.state,
        "arguments": operation.arguments,
        "definition_hash": operation.definition_hash,
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
        "approval_expires_at": (
            operation.approval_expires_at.isoformat()
            if operation.approval_expires_at is not None
            else None
        ),
        "execution_deadline": (
            operation.execution_deadline.isoformat()
            if operation.execution_deadline is not None
            else None
        ),
        "n8n_execution_id": operation.n8n_execution_id,
        "handle_used": operation.handle_burned_at is not None,
        "parent_operation_id": operation.parent_operation_id,
    }


@app.command("list")
def list_operations(
    workflow_id: str | None = typer.Option(None, "--workflow-id", help="Filter to one workflow."),
    state: list[str] | None = typer.Option(
        None, "--state", help="Repeatable; filter to these states."
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Max rows (1-100)."),
    as_json: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON instead of a table."
    ),
) -> None:
    """List this principal's own operations, most recently created first."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                operations = service.list_operations(
                    session,
                    principal_id=principal_id,
                    workflow_id=workflow_id,
                    states=state,
                    limit=limit,
                    enable_v2=enable_v2,
                )
        except InvalidArgumentsError as exc:
            typer.secho(exc.message, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        except OperationalError:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(json.dumps([_summary_dict(op) for op in operations], indent=2, sort_keys=True))
        return

    if not operations:
        typer.echo("No operations.")
        return

    table = Table()
    table.add_column("operation_id")
    table.add_column("workflow_id")
    table.add_column("state")
    table.add_column("created_at")
    table.add_column("parent_operation_id")
    for operation in operations:
        table.add_row(
            operation.id,
            operation.workflow_id,
            operation.state,
            operation.created_at.isoformat(),
            operation.parent_operation_id or "",
        )
    # A fixed, generous width rather than terminal auto-detection: operation IDs are
    # 26-character ULIDs, and a narrow or non-TTY width (the common case for a piped
    # or captured invocation) would otherwise truncate the one column that identifies
    # the row at all.
    Console(width=200).print(table)


@app.command("show")
def show(
    operation_id: str = typer.Argument(..., help="The operation to show."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show one operation's current state, redacted arguments, and deadlines."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return

    if as_json:
        typer.echo(json.dumps(_detail_dict(operation), indent=2, sort_keys=True))
        return

    typer.echo(f"operation_id:        {operation.id}")
    typer.echo(f"workflow_id:         {operation.workflow_id}")
    typer.echo(f"environment:         {operation.environment}")
    typer.echo(f"state:               {operation.state}")
    typer.echo(f"arguments:           {json.dumps(operation.arguments, indent=2, sort_keys=True)}")
    typer.echo(f"definition_hash:     {operation.definition_hash}")
    typer.echo(f"created_at:          {operation.created_at.isoformat()}")
    typer.echo(f"updated_at:          {operation.updated_at.isoformat()}")
    if operation.approval_expires_at is not None:
        typer.echo(f"approval_expires_at: {operation.approval_expires_at.isoformat()}")
    if operation.execution_deadline is not None:
        typer.echo(f"execution_deadline:  {operation.execution_deadline.isoformat()}")
    if operation.n8n_execution_id is not None:
        typer.echo(f"n8n_execution_id:    {operation.n8n_execution_id}")
    typer.echo(f"handle_used:         {operation.handle_burned_at is not None}")
    if operation.parent_operation_id is not None:
        typer.echo(f"parent_operation_id: {operation.parent_operation_id}")


@app.command("cancel")
def cancel(
    operation_id: str = typer.Argument(..., help="The operation to cancel."),
    reason: str | None = typer.Option(None, "--reason", help="Recorded on the audit trail."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Withdraw a ``PENDING_APPROVAL`` or ``APPROVED`` operation before it runs."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return

        typer.echo(f"operation_id: {operation.id}")
        typer.echo(f"workflow_id:  {operation.workflow_id}")
        typer.echo(f"state:        {operation.state}")

        if not yes and not typer.confirm("Cancel this operation?"):
            typer.echo("Not canceled.")
            raise typer.Exit(code=1)

        try:
            with session_scope(session_factory) as session:
                updated = service.cancel_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    reason=reason,
                    enable_v2=enable_v2,
                )
        except InvalidStateTransitionError:
            typer.secho(
                f"This operation is {operation.state}; only PENDING_APPROVAL or APPROVED "
                "operations can be canceled.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    typer.secho(f"Canceled. state={updated.state}", fg=typer.colors.GREEN)


@app.command("approval-status")
def approval_status(
    operation_id: str = typer.Argument(..., help="The operation to inspect."),
) -> None:
    """Render one operation's approval decision surface: workflow, risk, side-effect
    class, redacted arguments, drift status, deadlines, and — if decided — who decided
    it and when."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                context = service.get_approval_decision_context(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return
    _render_context(context)


@app.command("request-approval")
def request_approval(
    operation_id: str = typer.Argument(..., help="The operation to route for approval."),
    approvers: str | None = typer.Option(
        None,
        "--approvers",
        help="Comma-separated principal IDs — a subset of the operation's own approval-"
        "policy snapshot. Omit to notify every eligible approver.",
    ),
    message: str | None = typer.Option(
        None, "--message", help="Advisory text shown alongside the notification (ADR-007)."
    ),
) -> None:
    """Route a pending operation's approval to its eligible approvers and send
    notifications. Routing only — this command can never approve, choose a weaker
    quorum, or add an approver outside the operation's own snapshot."""
    approver_list = [a.strip() for a in approvers.split(",") if a.strip()] if approvers else None
    with _connected() as session_factory:
        try:
            sink = _cli_notification_sink()
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                result = service.request_approval(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    sink=sink,
                    approvers=approver_list,
                    message=message,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return
        except ApproverNotInPolicyError as exc:
            typer.secho(f"Not eligible approvers: {exc.details}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except InvalidStateTransitionError:
            typer.secho(
                "This operation is not PENDING_APPROVAL; nothing to route.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    typer.secho(f"Routed. notified={result.notified}", fg=typer.colors.GREEN)
    typer.echo(f"quorum_count:             {result.quorum_count}")
    typer.echo(f"approval_policy_snapshot: {result.approval_policy_snapshot}")


@app.command("approve")
def approve(
    operation_id: str = typer.Argument(..., help="The operation to approve."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Render the pending operation and, on confirmation, approve it (T06) — the
    canonical v1 approval channel (ADR-010). You cannot approve an operation you
    yourself are the model asking about; this command is for the human at the
    keyboard."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                context = service.get_approval_decision_context(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return

        _render_context(context)

        if context.state != "PENDING_APPROVAL":
            typer.secho(
                f"This operation is {context.state}, not PENDING_APPROVAL; nothing to approve.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

        if not yes and not typer.confirm("Approve this operation?"):
            typer.echo("Not approved.")
            raise typer.Exit(code=1)

        try:
            with session_scope(session_factory) as session:
                operation = service.approve_operation(
                    session,
                    operation_id=operation_id,
                    decided_by=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            typer.secho(
                "You are not authorized to decide this operation (or it does not "
                "exist) — an approver may not decide their own request.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
        except InvalidStateTransitionError:
            typer.secho(
                "This operation changed state while you were deciding; run "
                "`n8n-operator operations approval-status` to see its current state.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    typer.secho(f"Approved. state={operation.state}", fg=typer.colors.GREEN)


@app.command("reject")
def reject(
    operation_id: str = typer.Argument(..., help="The operation to reject."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Render the pending operation and, on confirmation, reject it (T07)."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                context = service.get_approval_decision_context(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return

        _render_context(context)

        if context.state != "PENDING_APPROVAL":
            typer.secho(
                f"This operation is {context.state}, not PENDING_APPROVAL; nothing to reject.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

        if not yes and not typer.confirm("Reject this operation?"):
            typer.echo("Not rejected.")
            raise typer.Exit(code=1)

        try:
            with session_scope(session_factory) as session:
                operation = service.reject_operation(
                    session,
                    operation_id=operation_id,
                    decided_by=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            typer.secho(
                "You are not authorized to decide this operation (or it does not "
                "exist) — an approver may not decide their own request.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
        except InvalidStateTransitionError:
            typer.secho(
                "This operation changed state while you were deciding; run "
                "`n8n-operator operations approval-status` to see its current state.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    typer.secho(f"Rejected. state={operation.state}", fg=typer.colors.GREEN)


@app.command("expire")
def expire() -> None:
    """Apply every overdue approval/execution deadline now, across every principal.

    A maintenance convenience for a schedule (cron, a systemd timer) in a deployment
    that runs no approval app and wants ``EXPIRED`` audit events to land near the
    deadline rather than at whatever moment something next reads the row — lazy
    transactional expiry already makes this safe to skip entirely (invariant I9).
    Idempotent: running it twice in a row, or concurrently with another sweep, never
    double-transitions or errors on a row someone else just expired.
    """
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                count = service.expire_overdue_operations(session)
        except OperationalError:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
    typer.echo(f"Expired {count} operation(s).")


# ----------------------------------------------------------------------------------
# reconcile — the one place in this file that needs a real n8n connection (stage 06,
# ADR-009/ADR-012). Every other command above deliberately doesn't (this module's own
# docstring) — reconciliation is the one exception, since confirming what n8n actually
# did with an `UNKNOWN` operation is the entire point.
# ----------------------------------------------------------------------------------

reconcile_app = typer.Typer(
    help="Record and list exact-ID reconciliation evidence for UNKNOWN operations.",
    no_args_is_help=True,
)
app.add_typer(reconcile_app, name="reconcile")


class _CliReconciliationAdapter:
    """Converts ``n8n.client.N8nClient.get_execution``'s ``n8n.types.ExecutionSummary``
    into ``core.models.ExecutionLookup`` — the same real-type-behind-a-port conversion
    ``cli/commands/health.py``'s ``_CliHealthAdapter`` performs, duplicated here in
    miniature rather than imported (this command is its own composition root)."""

    def __init__(self, client: N8nClient) -> None:
        self._client = client

    def get_execution(self, execution_id: str) -> ExecutionLookup:
        raw = self._client.get_execution(execution_id)
        return ExecutionLookup(
            execution_id=raw.id, n8n_workflow_id=raw.workflow_id, status=raw.status
        )


def _n8n_client_for_operation(
    session: Session, operation: Operation, *, enable_v2: bool
) -> N8nClient:
    """The n8n instance an operation's own execution actually ran against: that one
    environment's own credentials in v2 (``operation.environment`` is the resolved
    environment's own ID, exactly as ``core.service._prepare_or_retry`` stores it —
    never the free-text v1 field in that mode), or the single process-wide instance in
    v1 (``load_settings()``, the same full configuration ``serve.py``'s ``stdio``/
    ``http`` already require)."""
    if enable_v2:
        environment = EnvironmentRepository(session).get(operation.environment)
        if environment is not None:
            base_url = resolve_secret_reference(environment.n8n_base_url_ref)
            api_key = resolve_secret_reference(environment.n8n_api_key_ref)
            register_secret(api_key)
            return N8nClient(base_url=base_url, api_key=api_key, connect_timeout_seconds=60.0)
    settings = load_settings()
    register_secret(settings.n8n_api_key.get_secret_value())
    return N8nClient(
        base_url=str(settings.n8n_base_url),
        api_key=settings.n8n_api_key.get_secret_value(),
        connect_timeout_seconds=float(settings.request_timeout_seconds),
    )


def _print_reconciliation_record(record: ReconciliationRecord) -> None:
    typer.echo(f"operation_id:         {record.operation_id}")
    typer.echo(f"execution_id:         {record.execution_id}")
    typer.echo(f"n8n_workflow_id:      {record.n8n_workflow_id}")
    typer.echo(f"n8n_execution_status: {record.n8n_execution_status}")
    typer.echo(f"note:                 {record.note}")
    typer.echo(f"actor:                {record.actor}")
    typer.echo(f"recorded_at:          {record.recorded_at.isoformat()}")


@reconcile_app.command("record")
def reconcile_record(
    operation_id: str = typer.Argument(..., help="The UNKNOWN operation to reconcile."),
    execution_id: str = typer.Option(..., "--execution-id", help="The n8n execution ID."),
    note: str = typer.Option(
        ..., "--note", help='What you are asserting (e.g. "confirmed via n8n UI: succeeded").'
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Record exact-ID reconciliation evidence. Never changes the operation's own
    state — UNKNOWN stays UNKNOWN forever (invariant I7); this only appends one
    audit-log annotation. Requires an exact n8n execution ID and an explicit human
    note — never inferred from elapsed time."""
    with _connected() as session_factory:
        with session_scope(session_factory) as session:
            principal_id, enable_v2 = _resolve_principal(session)
            try:
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
            except OperationNotFoundError:
                _operation_not_found_or_exit(operation_id)
                return
            client = _n8n_client_for_operation(session, operation, enable_v2=enable_v2)

        typer.echo(f"operation_id:  {operation_id}")
        typer.echo(f"current state: {operation.state}")
        typer.echo(f"execution_id:  {execution_id}")
        typer.echo(f"note:          {note}")
        if not yes and not typer.confirm(
            "Record this as verified reconciliation evidence? This never changes the "
            "operation's own state."
        ):
            typer.echo("Not recorded.")
            raise typer.Exit(code=1)

        try:
            with session_scope(session_factory) as session:
                record = service.reconcile_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    execution_id=execution_id,
                    note=note,
                    reconciliation=_CliReconciliationAdapter(client),
                    enable_v2=enable_v2,
                )
        except OperationNotFoundError:
            typer.secho(
                "You are not authorized to reconcile this operation (or it does not exist).",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
        except ReconciliationNotApplicableError as exc:
            typer.secho(exc.message, fg=typer.colors.RED, err=True)
            typer.secho(f"details: {exc.details}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    typer.secho("Recorded.", fg=typer.colors.GREEN)
    _print_reconciliation_record(record)


@reconcile_app.command("list")
def reconcile_list(
    operation_id: str = typer.Argument(..., help="The operation to list evidence for."),
) -> None:
    """List every reconciliation annotation recorded for one operation, oldest
    first."""
    with _connected() as session_factory, session_scope(session_factory) as session:
        principal_id, enable_v2 = _resolve_principal(session)
        try:
            records = service.list_reconciliation_events(
                session, operation_id=operation_id, principal_id=principal_id, enable_v2=enable_v2
            )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return

    if not records:
        typer.echo("No reconciliation evidence recorded.")
        return
    for record in records:
        _print_reconciliation_record(record)
        typer.echo("")


__all__ = ["app"]
