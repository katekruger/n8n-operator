"""``n8n-operator registry`` — validate, list, show, hash, reload.

``validate``, ``list``, ``show``, and ``hash`` are pure, file-only operations: they call
``registry/loader.py`` directly and touch no database, since there is no policy decision
or cross-capability orchestration involved in reporting what a file on disk contains.
``reload`` is the one command that persists a snapshot, so it goes through
``core.service.reload_registry`` — the only place permitted to depend on both
``registry/`` and ``storage/`` at once (ARCHITECTURE.md section 2.1).

None of these commands require ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY``
to be set: the registry path and the argument-size ceiling are resolved via
:func:`~n8n_operator.config.resolve_registry_path` and
:func:`~n8n_operator.config.resolve_max_argument_bytes`, which — like
:func:`~n8n_operator.config.resolve_database_url` in phase 1 — never require the rest of
:class:`~n8n_operator.config.Settings` to validate.

``registry hash`` computes the registry **document's** canonical content hash (BUILD_PLAN
section 6.7) — a file-only, n8n-free operation. WORKFLOW_REGISTRY.md section 5 also
describes a second mode, ``--n8n-workflow-id``, that computes a specific *workflow's*
``definition_hash`` by fetching its live definition from n8n; that mode is out of scope
here (phase 2 implements no n8n network calls) and arrives with n8n integration in
phase 4. Passing ``--n8n-workflow-id`` now reports that plainly rather than being
silently ignored.

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from n8n_operator.config import resolve_max_argument_bytes, resolve_registry_path
from n8n_operator.registry.loader import (
    LoadedRegistry,
    RegistryParseError,
    RegistryValidationError,
    load_registry,
)
from n8n_operator.registry.schema import WorkflowEntry

app = typer.Typer(help="Inspect and manage the workflow registry.", no_args_is_help=True)

_PATH_OPTION = typer.Option(None, "--path", help="Registry file path (default: configured path).")
_MAX_ARG_BYTES_OPTION = typer.Option(
    None, "--server-max-argument-bytes", help="Override the server argument-size ceiling (R11)."
)


def _resolve_path(path: str | None) -> Path:
    return resolve_registry_path(path)


def _load_or_exit(path: Path, *, server_max_argument_bytes: int | None) -> LoadedRegistry:
    limit = resolve_max_argument_bytes(server_max_argument_bytes)
    try:
        return load_registry(path, server_max_argument_bytes=limit)
    except RegistryValidationError as exc:
        typer.secho(
            f"Registry is invalid ({len(exc.violations)} problem(s)):",
            fg=typer.colors.RED,
            err=True,
        )
        for violation in exc.violations:
            typer.echo(f"  {violation.format()}", err=True)
        raise typer.Exit(code=1) from exc
    except RegistryParseError as exc:
        typer.secho(f"Registry could not be parsed: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _find_entry(loaded: LoadedRegistry, workflow_id: str) -> WorkflowEntry:
    for entry in loaded.entries:
        if entry.id == workflow_id:
            return entry
    typer.secho(f"No such workflow: {workflow_id!r}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command("validate")
def validate(
    path: str | None = _PATH_OPTION,
    server_max_argument_bytes: int | None = _MAX_ARG_BYTES_OPTION,
) -> None:
    """Load and validate the registry. Exits non-zero and names every offending rule
    and entry on any violation (AC-02) — meant to run in CI on the repository holding
    the registry (WORKFLOW_REGISTRY.md section 9.1)."""
    resolved_path = _resolve_path(path)
    loaded = _load_or_exit(resolved_path, server_max_argument_bytes=server_max_argument_bytes)
    enabled = sum(1 for e in loaded.entries if e.enabled)
    typer.secho("Registry is valid.", fg=typer.colors.GREEN)
    typer.echo(f"  path:          {resolved_path}")
    typer.echo(f"  content_hash:  {loaded.content_hash}")
    typer.echo(f"  workflows:     {len(loaded.entries)} ({enabled} enabled)")


@app.command("list")
def list_workflows(
    path: str | None = _PATH_OPTION,
    server_max_argument_bytes: int | None = _MAX_ARG_BYTES_OPTION,
) -> None:
    """List every workflow entry in the registry (including disabled ones, marked as
    such) — operator-facing, unlike the MCP ``list_workflows`` tool, which excludes
    disabled entries entirely."""
    resolved_path = _resolve_path(path)
    loaded = _load_or_exit(resolved_path, server_max_argument_bytes=server_max_argument_bytes)
    if not loaded.entries:
        typer.echo("(no workflows registered)")
        return
    for entry in loaded.entries:
        marker = "" if entry.enabled else " [disabled]"
        typer.echo(
            f"{entry.id}{marker}  risk={entry.risk}  side_effects={entry.side_effects}  "
            f'approval={entry.approval}  "{entry.title}"'
        )


@app.command("show")
def show(
    workflow_id: str = typer.Argument(..., help="The registry id to show."),
    path: str | None = _PATH_OPTION,
    server_max_argument_bytes: int | None = _MAX_ARG_BYTES_OPTION,
) -> None:
    """Show one workflow's full entry.

    Runs on the operator's own machine against a file the operator already has direct
    read access to, so this deliberately shows everything in the entry — including
    ``n8n_workflow_id`` and ``trigger.secret_ref`` (a reference, e.g. ``env:NAME``,
    never a resolved secret value; registry loading never touches the secret itself).
    This is *not* what an MCP client sees: that boundary is
    :class:`~n8n_operator.registry.schema.WorkflowDetail`, built for a later phase's
    ``describe_workflow`` tool, whose field set structurally excludes both (boundary B5).
    """
    resolved_path = _resolve_path(path)
    loaded = _load_or_exit(resolved_path, server_max_argument_bytes=server_max_argument_bytes)
    entry = _find_entry(loaded, workflow_id)

    typer.echo(f"id:               {entry.id}")
    typer.echo(f"enabled:          {entry.enabled}")
    typer.echo(f"title:            {entry.title}")
    typer.echo(f"description:      {entry.description}")
    typer.echo(f"owner:            {entry.owner}")
    typer.echo(f"version:          {entry.version}")
    typer.echo(f"risk:             {entry.risk}")
    typer.echo(f"side_effects:     {entry.side_effects}")
    typer.echo(f"approval:         {entry.approval}")
    typer.echo(f"tags:             {', '.join(entry.tags) or '(none)'}")
    typer.echo(f"n8n_workflow_id:  {entry.n8n_workflow_id}")
    typer.echo(f"definition_hash:  {entry.definition_hash}")
    typer.echo("trigger:")
    typer.echo(f"  type:           {entry.trigger.type}")
    typer.echo(f"  method:         {entry.trigger.method}")
    typer.echo(f"  path:           {entry.trigger.path}")
    typer.echo(f"  auth:           {entry.trigger.auth}")
    typer.echo(f"  secret_ref:     {entry.trigger.secret_ref or '(none)'}")
    typer.echo(f"  correlation:    {entry.trigger.correlation}")
    typer.echo("limits:")
    typer.echo(f"  timeout_seconds:       {entry.limits.timeout_seconds}")
    typer.echo(f"  approval_ttl_seconds:  {entry.limits.approval_ttl_seconds}")
    typer.echo(f"  execution_ttl_seconds: {entry.limits.execution_ttl_seconds}")
    typer.echo(f"  max_concurrent:        {entry.limits.max_concurrent}")
    typer.echo(f"  rate_limit_per_minute: {entry.limits.rate_limit_per_minute}")
    typer.echo(f"  max_argument_bytes:    {entry.limits.max_argument_bytes}")
    typer.echo("output:")
    typer.echo(f"  max_bytes:          {entry.output.max_bytes}")
    typer.echo(f"  include_node_trace: {entry.output.include_node_trace}")
    typer.echo(f"  redact:             {entry.output.redact or '(none)'}")
    typer.echo("input_schema:")
    typer.echo(json.dumps(entry.input_schema, indent=2, sort_keys=True))


@app.command("hash")
def hash_(
    path: str | None = _PATH_OPTION,
    server_max_argument_bytes: int | None = _MAX_ARG_BYTES_OPTION,
    n8n_workflow_id: str | None = typer.Option(
        None,
        "--n8n-workflow-id",
        help="Not yet implemented — arrives with n8n integration in phase 4.",
    ),
) -> None:
    """Print the registry document's canonical content hash (BUILD_PLAN section 6.7) —
    the same hash ``registry reload`` would create a snapshot for."""
    if n8n_workflow_id is not None:
        typer.secho(
            "--n8n-workflow-id is not yet implemented: computing a workflow's own "
            "definition_hash requires fetching its live definition from n8n, which "
            "arrives with n8n integration in phase 4. This command currently computes "
            "the registry document's own content hash only.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)
    resolved_path = _resolve_path(path)
    loaded = _load_or_exit(resolved_path, server_max_argument_bytes=server_max_argument_bytes)
    typer.echo(loaded.content_hash)


@app.command("reload")
def reload_(
    path: str | None = _PATH_OPTION,
    server_max_argument_bytes: int | None = _MAX_ARG_BYTES_OPTION,
) -> None:
    """Validate the registry and, only if it is entirely clean, persist a new active
    snapshot (BUILD_PLAN section 6.7, WORKFLOW_REGISTRY.md section 9.2).

    A failed validation leaves the previously-active snapshot untouched — this command
    fully validates before it ever opens a database transaction (see
    ``core.service.reload_registry``'s docstring for the atomicity and immutability
    guarantees).
    """
    from sqlalchemy.exc import OperationalError

    from n8n_operator.config import resolve_database_url
    from n8n_operator.core.service import reload_registry
    from n8n_operator.storage.session import (
        create_engine_for_url,
        create_session_factory,
        session_scope,
    )

    resolved_path = _resolve_path(path)
    # Validate first, entirely outside any database transaction — a failure here never
    # touches storage at all, let alone partially.
    _load_or_exit(resolved_path, server_max_argument_bytes=server_max_argument_bytes)

    # The schema is created by migrations, never by create_all outside tests (ADR-004
    # rule D6) — `reload` assumes `n8n-operator db init` (or `db migrate`) has already
    # run, exactly like every other command that touches application data.
    database_url = resolve_database_url()
    engine = create_engine_for_url(database_url)
    try:
        session_factory = create_session_factory(engine)
        try:
            with session_scope(session_factory) as session:
                snapshot, reused = reload_registry(
                    session,
                    resolved_path,
                    server_max_argument_bytes=resolve_max_argument_bytes(server_max_argument_bytes),
                )
        except OperationalError as exc:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if reused:
        typer.secho(
            "Registry unchanged; the active snapshot was already current.", fg=typer.colors.GREEN
        )
    else:
        typer.secho("Registry reloaded; a new snapshot is now active.", fg=typer.colors.GREEN)
    typer.echo(f"  snapshot_id:   {snapshot.id}")
    typer.echo(f"  content_hash:  {snapshot.content_hash}")


__all__ = ["app"]
