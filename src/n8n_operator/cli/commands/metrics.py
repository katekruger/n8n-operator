"""``n8n-operator metrics`` — bounded, authorization-filtered operational metrics
(stage 08, MCP_TOOLS.md section 5.7, ADR-019).

Never requires ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY`` — this reads
only ``operations``/``execution_results``, never n8n itself, the same "governance state
is orthogonal to n8n reachability" reasoning ``audit.py``/``operations.py`` already
document.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import typer
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import resolve_database_url, resolve_v2_identity_flags
from n8n_operator.core import service
from n8n_operator.core.identity import resolve_cli_principal_id
from n8n_operator.errors import OperatorError
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="Bounded, authorization-filtered operational metrics.", no_args_is_help=True)


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@app.command("show")
def show(
    environment: str | None = typer.Option(
        None, "--environment", help="Environment id (v2 only; standard resolution)."
    ),
    window: str = typer.Option("24h", "--window", help="One of 1h, 24h, 7d, 30d."),
    group_by: str | None = typer.Option(
        None, "--group-by", help="One of workflow, risk, side_effects, outcome."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the full machine-readable result."),
) -> None:
    """Operation counts, outcome distribution, and execution-latency percentiles over
    an enumerated window — never a caller-supplied arbitrary time range (ADR-019)."""
    enable_v2, dev_principal_id = resolve_v2_identity_flags()
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id = resolve_cli_principal_id(
                    session, enable_v2=enable_v2, dev_principal_id=dev_principal_id
                )
                result = service.get_metrics(
                    session,
                    principal_id=principal_id,
                    environment=environment,
                    window=window,
                    group_by=group_by,
                    enable_v2=enable_v2,
                )
        except OperationalError:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
        except OperatorError as exc:
            typer.secho(exc.message, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        return

    typer.echo(f"window:          {result.window}")
    typer.echo(f"generated_at:    {result.generated_at.isoformat()}")
    typer.echo(f"total:           {result.totals.count}")
    for outcome, count in sorted(result.totals.by_outcome.items()):
        typer.echo(f"  {outcome}: {count}")
    latency = result.latency_ms
    for label, value, reason in (
        ("p50", latency.p50, latency.p50_reason),
        ("p95", latency.p95, latency.p95_reason),
        ("p99", latency.p99, latency.p99_reason),
    ):
        typer.echo(f"latency_ms {label}: {value if value is not None else f'null ({reason})'}")
    if result.breakdown:
        typer.echo("breakdown:")
        for entry in result.breakdown:
            note = f"  ({entry.note})" if entry.note else ""
            typer.echo(f"  {entry.key}: {entry.count}{note}")


__all__ = ["app"]
