"""``n8n-operator operations`` — approve, reject, expire, approval-status.

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

None of these commands requires ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY``
to be set — approving, rejecting, and expiring are pure Operator-state operations that
never call n8n, so the operator can act on them even while n8n itself is unreachable
(the same "schema management is orthogonal" reasoning ``db.py``/``registry.py`` already
document, applied here to "governance state management").

``list``, ``show``, and ``cancel`` arrive in phase 8.

Phase 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import typer
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import resolve_database_url
from n8n_operator.core import service
from n8n_operator.core.models import ApprovalDecisionContext
from n8n_operator.errors import InvalidStateTransitionError, OperationNotFoundError
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="Approve, reject, and expire pending operations.", no_args_is_help=True)

_PRINCIPAL_ID = "local"  # v1 has exactly one principal (BUILD_PLAN section 8.1)


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
    else:
        typer.echo(f"definition_hash:     {context.registered_definition_hash} (unchanged)")
    typer.echo(f"created_at:          {context.created_at.isoformat()}")
    if context.approval_expires_at is not None:
        typer.echo(f"approval_expires_at: {context.approval_expires_at.isoformat()}")
    if context.execution_deadline is not None:
        typer.echo(f"execution_deadline:  {context.execution_deadline.isoformat()}")
    if not context.approval_required:
        typer.echo("approval:            not required (read_only + approval: none)")
    elif context.decided:
        typer.echo(
            f"approval:            {context.decision} by {context.decided_by} "
            f"at {context.decided_at.isoformat() if context.decided_at else '?'}"
        )
    else:
        typer.echo("approval:            pending")


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
                context = service.get_approval_decision_context(
                    session, operation_id=operation_id, principal_id=_PRINCIPAL_ID
                )
        except OperationNotFoundError:
            _operation_not_found_or_exit(operation_id)
            return
    _render_context(context)


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
                context = service.get_approval_decision_context(
                    session, operation_id=operation_id, principal_id=_PRINCIPAL_ID
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
                    session, operation_id=operation_id, decided_by=_PRINCIPAL_ID
                )
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
                context = service.get_approval_decision_context(
                    session, operation_id=operation_id, principal_id=_PRINCIPAL_ID
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
                    session, operation_id=operation_id, decided_by=_PRINCIPAL_ID
                )
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


__all__ = ["app"]
