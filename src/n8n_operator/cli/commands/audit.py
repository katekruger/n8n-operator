"""``n8n-operator audit`` — verify, export.

``audit verify`` walks the hash chain and reports the first break by sequence number
(AC-22). ``audit export`` produces a complete, chain-verifiable record: every audit
entry, every operation's state transitions, and the registry snapshots those operations
were governed against — enough for a separate process to independently re-verify the
chain and reconstruct what happened, with arguments redacted per each workflow's own
``output.redact`` policy and no approval token, n8n credential, or webhook secret ever
included (AC-25; BUILD_PLAN section 9.4; see ``core.service.export_audit_record``'s own
docstring for exactly what is and is not in the export, and why).

Neither command requires ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY`` —
the same "governance state is orthogonal to n8n reachability" reasoning
``operations.py`` already documents for approve/reject/expire, applied here to
inspecting and exporting that state's own audit trail.

Phase 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import resolve_database_url, resolve_v2_identity_flags
from n8n_operator.core import service
from n8n_operator.core.identity import resolve_cli_principal_id
from n8n_operator.errors import InsufficientRoleError
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="Verify and export the audit trail.", no_args_is_help=True)

EXIT_CHAIN_BROKEN = 2
"""``audit verify``'s exit code when the chain does not verify — distinct from ``1``
(a general/usage error, e.g. an uninitialized database) so a monitoring script can tell
"the audit trail was tampered with" apart from "this command was invoked wrong"."""


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _database_not_initialized_or_exit() -> None:
    typer.secho(
        "Database is not initialized — run `n8n-operator db init` first.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _resolve_principal(session: Session) -> tuple[str, bool]:
    """``(principal_id, enable_v2)`` for the current invocation (Stage 03) — v1 (the
    default) resolves the fixed ``"local"`` identity; v2 resolves the CLI's own
    identity (``core.identity.resolve_cli_principal_id``), used here to gate these
    system-wide, cross-principal commands to the ``admin`` role
    (``core.service._require_admin``)."""
    enable_v2, dev_principal_id = resolve_v2_identity_flags()
    principal_id = resolve_cli_principal_id(
        session, enable_v2=enable_v2, dev_principal_id=dev_principal_id
    )
    return principal_id, enable_v2


def _insufficient_role_or_exit() -> None:
    typer.secho(
        "This command requires the admin role.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


@app.command("verify")
def verify(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Walk the audit hash chain and report whether it is intact.

    A clean database exits ``0``. A broken chain — any row whose stored content no
    longer matches its own hash, or whose ``prev_hash`` no longer matches the entry
    before it — exits ``2`` and names the exact sequence number where verification
    first failed (AC-22). This is tamper-*evidence*, not tamper-*proofing*: it proves a
    row was changed, not who changed it or what it said before.
    """
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                result = service.verify_audit_chain(
                    session, principal_id=principal_id, enable_v2=enable_v2
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except InsufficientRoleError:
            _insufficient_role_or_exit()
            return

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": result.ok,
                    "first_break_seq": result.first_break_seq,
                    "reason": result.reason,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif result.ok:
        typer.secho("OK — the audit chain is intact.", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"BROKEN at seq={result.first_break_seq}: {result.reason}",
            fg=typer.colors.RED,
            err=True,
        )

    if not result.ok:
        raise typer.Exit(code=EXIT_CHAIN_BROKEN)


@app.command("export")
def export(
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write to this file instead of stdout."
    ),
) -> None:
    """Produce a complete, chain-verifiable, redacted record of every operation and the
    full audit log (AC-25) — see this module's docstring for exactly what is and is not
    included. Always JSON, with sorted keys, so two exports of the same underlying
    state produce byte-identical *data* (the ``exported_at`` timestamp is the one
    field that necessarily differs run to run).
    """
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                record = service.export_audit_record(
                    session, principal_id=principal_id, enable_v2=enable_v2
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except InsufficientRoleError:
            _insufficient_role_or_exit()
            return

    payload = json.dumps(record, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(
            f"Wrote {len(record['operations'])} operation(s) and "
            f"{len(record['audit_log'])} audit entries to {output}."
        )
    else:
        typer.echo(payload)

    if not record["chain"]["ok"]:
        typer.secho(
            f"WARNING: the exported audit chain is broken at "
            f"seq={record['chain']['first_break_seq']}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_CHAIN_BROKEN)


@app.command("list")
def list_events(
    environment: str | None = typer.Option(
        None, "--environment", help="Environment id (v2 only; standard resolution)."
    ),
    workflow_id: str | None = typer.Option(None, "--workflow-id", help="Filter to one workflow."),
    since: str | None = typer.Option(None, "--since", help="RFC 3339 timestamp."),
    limit: int = typer.Option(20, "--limit", help="1-100, default 20."),
    cursor: str | None = typer.Option(None, "--cursor", help="Opaque pagination cursor."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Query the audit chain within the caller's own authorization scope (stage 08,
    MCP_TOOLS.md section 5.8, ADR-012 section 3) — unlike ``verify``/``export``, this
    is not admin-only: any role can run it, scoped to whatever workflows/environments
    that role's own grants cover (``viewer`` included, same as ``list_audit_events``'s
    own role matrix entry)."""
    from n8n_operator.errors import OperatorError

    since_dt = None
    if since is not None:
        try:
            from datetime import datetime

            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            typer.secho(
                f"'--since' is not a valid RFC 3339 timestamp: {since!r}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                page = service.list_audit_events(
                    session,
                    principal_id=principal_id,
                    environment=environment,
                    workflow_id=workflow_id,
                    since=since_dt,
                    limit=limit,
                    cursor=cursor,
                    enable_v2=enable_v2,
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except OperatorError as exc:
            typer.secho(exc.message, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "events": [e.model_dump(mode="json") for e in page.events],
                    "next_cursor": page.next_cursor,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not page.events:
        typer.echo("(no audit events in scope)")
        return
    for event in page.events:
        typer.echo(
            f"seq={event.seq} {event.occurred_at.isoformat()} actor={event.actor} "
            f"action={event.action} subject={event.subject_type}:{event.subject_id} "
            f"outcome={event.outcome}"
        )
    if page.next_cursor is not None:
        typer.echo(f"next_cursor: {page.next_cursor}")


__all__ = ["app"]
