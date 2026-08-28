"""``n8n-operator db`` — init, migrate, status, migrate-to-postgres.

Every command resolves the database URL via
:func:`n8n_operator.config.resolve_database_url`, deliberately *not* through the full
:class:`~n8n_operator.config.Settings` — schema management is an orthogonal concern from
the rest of the application's configuration, and requiring ``N8N_BASE_URL``/
``N8N_API_KEY`` just to run ``db init`` on a fresh clone would conflate the two (the same
reasoning ``storage/migrations/env.py`` documents).

Alembic is driven programmatically via ``alembic.command`` against a ``Config`` built
entirely in Python — no ``alembic.ini`` is read at runtime — so these commands behave
identically whether invoked from a source checkout or an installed package, and
regardless of the process's current working directory.

``init``/``migrate`` also seed the v1 default principal (``id="local"``, the constant
every adapter and test in this codebase writes as ``ToolDeps.principal_id`` /
``_PRINCIPAL_ID``) if it does not already exist — a genuine gap found in phase 9
release verification: nothing else in the shipped product ever creates this row, so a
real fresh install's very first ``prepare_operation`` failed a ``principals`` foreign
key. v1 has exactly one principal (BUILD_PLAN section 8.1); seeding it here, in the
one-time setup command, is the whole fix — no other code path needed to change.

Phase 1 (BUILD_PLAN section 12); default-principal seeding added in phase 9.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import n8n_operator.storage as storage_package
from n8n_operator.config import (
    compose_database_url,
    redact_database_url,
    resolve_database_password,
    resolve_database_url,
)
from n8n_operator.core.postgres_migration import MigrationReport
from n8n_operator.core.postgres_migration import migrate as run_postgres_migration
from n8n_operator.storage.health import check_database_health
from n8n_operator.storage.postgres_migration import DEFAULT_CHUNK_SIZE, MigrationRefusedError
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="Manage the operator's database schema.", no_args_is_help=True)

DEFAULT_PRINCIPAL_ID = "local"


def _ensure_default_principal(database_url: str) -> bool:
    """Idempotently create the v1 default principal. Returns whether it was created —
    ``False`` means it already existed, not that anything failed."""
    engine = create_engine_for_url(database_url)
    try:
        session_factory = create_session_factory(engine)
        with session_scope(session_factory) as session:
            repo = PrincipalRepository(session)
            if repo.get(DEFAULT_PRINCIPAL_ID) is not None:
                return False
            repo.create(id=DEFAULT_PRINCIPAL_ID, kind="local", display_name="local")
            return True
    finally:
        engine.dispose()


def _migrations_dir() -> Path:
    """The migrations directory, resolved relative to the installed package — not the
    repository root — so this works the same in a source checkout and an installed
    wheel."""
    storage_file = storage_package.__file__
    if storage_file is None:  # pragma: no cover - defensive; always set for a real package
        raise RuntimeError("cannot locate the n8n_operator.storage package on disk")
    return Path(storage_file).resolve().parent / "migrations"


def _alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _ensure_sqlite_parent_exists(database_url: str) -> None:
    """Create the parent directory of a file-based SQLite database if it is missing.

    A no-op for every other case (an in-memory database, or any non-SQLite driver) —
    PostgreSQL in v2 has no filesystem path for this command to prepare.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    database = url.database
    if not database or database == ":memory:":
        return
    Path(database).resolve().parent.mkdir(parents=True, exist_ok=True)


def _resolve_database_url_or_exit() -> str:
    try:
        return resolve_database_url()
    except ValueError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("init")
def init() -> None:
    """Create the database (if needed), bring its schema to head, and seed the v1
    default principal.

    Safe to re-run: an already-initialized database at head, with its principal
    already seeded, is reported as such, not treated as an error.
    """
    database_url = _resolve_database_url_or_exit()
    _ensure_sqlite_parent_exists(database_url)
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    _ensure_default_principal(database_url)
    typer.echo(f"Database initialized ({database_url}); schema is at head.")


@app.command("migrate")
def migrate() -> None:
    """Bring the database schema to head and seed the v1 default principal. Works
    from an empty database (AC-24)."""
    database_url = _resolve_database_url_or_exit()
    _ensure_sqlite_parent_exists(database_url)
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    _ensure_default_principal(database_url)
    typer.echo("Database schema is now at head.")


@app.command("status")
def status() -> None:
    """Report the database's current revision and whether it is at head.

    Exits non-zero when the database is not initialized or is behind head, so this can
    gate a script (``n8n-operator db status || n8n-operator db migrate``) as well as be
    read by a human.
    """
    database_url = _resolve_database_url_or_exit()
    cfg = _alembic_config(database_url)
    script = ScriptDirectory.from_config(cfg)
    head_revision = script.get_current_head()

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(connection)
            current_revision = migration_context.get_current_revision()
    except OperationalError:
        # Nothing to connect to yet — e.g. a SQLite file whose parent directory has
        # never been created by `db init`. That is "not initialized", not a crash: this
        # command exists precisely to report that state cleanly.
        current_revision = None
    finally:
        engine.dispose()

    typer.echo(f"database_url:     {redact_database_url(database_url)}")
    typer.echo(f"current revision: {current_revision or '(none — not initialized)'}")
    typer.echo(f"head revision:    {head_revision}")

    health_engine = create_engine_for_url(database_url)
    try:
        health = check_database_health(health_engine)
    finally:
        health_engine.dispose()
    if health.reachable:
        typer.echo(f"connectivity:     reachable ({health.dialect}, {health.latency_ms}ms)")
    else:
        typer.echo(f"connectivity:     unreachable ({health.error})")

    if current_revision == head_revision:
        typer.echo("status: up to date")
        return
    if current_revision is None:
        typer.echo("status: not initialized — run `n8n-operator db init`")
        raise typer.Exit(code=1)
    typer.echo("status: behind head — run `n8n-operator db migrate`")
    raise typer.Exit(code=1)


def _resolve_dest_url(dest: str) -> str:
    """Resolve a destination URL's password the same way :class:`Settings` does for
    ``database_url`` — ``N8N_OPERATOR_DATABASE_PASSWORD`` (``env:``/``keyring:``
    indirection, ADR-006) substituted in if the destination URL does not already carry
    a literal one."""
    try:
        password = resolve_database_password()
        return compose_database_url(dest, password)
    except ValueError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("migrate-to-postgres")
def migrate_to_postgres(
    dest: str = typer.Option(
        ...,
        "--dest",
        help="Destination PostgreSQL URL (e.g. postgresql+psycopg://user@host:5432/db). "
        "A password may be embedded, or supplied via N8N_OPERATOR_DATABASE_PASSWORD.",
    ),
    source: str | None = typer.Option(
        None, "--source", help="Source SQLite URL. Defaults to the configured database_url."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the plan; write nothing to the destination."
    ),
    chunk_size: int = typer.Option(DEFAULT_CHUNK_SIZE, "--chunk-size", min=1),
    checkpoint: Path | None = typer.Option(
        None, "--checkpoint", help="Checkpoint file path. Defaults to a per-destination path."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Continue a prior interrupted run using its checkpoint."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print a machine-readable JSON report."),
) -> None:
    """Copy an existing v1 SQLite database's rows onto a PostgreSQL destination, once,
    with proof it worked (BUILD_PLAN section 8.3, ADR-004; v2 stage 01).

    Idempotent and checkpointed: an interrupted run's progress is on disk, and
    ``--resume`` continues it rather than starting over. Refuses a destination that
    already has rows in it unless resuming a checkpoint that matches the source exactly
    as it was. Exits ``0`` on a fully verified migration (every table's row count
    matches, and the destination's audit hash chain re-verifies intact), ``2`` if the
    copy completed but verification found a problem, and ``1`` on any refused or
    misconfigured attempt — never partway through a silent success.
    """
    resolved_dest = _resolve_dest_url(dest)
    resolved_source = source or _resolve_database_url_or_exit()

    try:
        report: MigrationReport = run_postgres_migration(
            source_url=resolved_source,
            dest_url=resolved_dest,
            dry_run=dry_run,
            chunk_size=chunk_size,
            checkpoint_path=checkpoint,
            resume=resume,
        )
    except MigrationRefusedError as exc:
        typer.secho(f"Migration refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "dry_run": report.dry_run,
                    "resumed": report.resumed,
                    "ok": report.ok,
                    "tables": [
                        {
                            "table": t.table_name,
                            "source_count": t.source_count,
                            "dest_count_before": t.dest_count_before,
                            "rows_copied": t.rows_copied,
                            "dest_count_after": t.dest_count_after,
                        }
                        for t in report.tables
                    ],
                    "audit_chain_ok": report.audit_chain.ok,
                    "audit_chain_first_break_seq": report.audit_chain.first_break_seq,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        label = "DRY RUN — " if report.dry_run else ""
        typer.echo(f"{label}source:      {redact_database_url(resolved_source)}")
        typer.echo(f"{label}destination: {redact_database_url(resolved_dest)}")
        for t in report.tables:
            typer.echo(
                f"  {t.table_name:<32} source={t.source_count:<8} "
                f"copied={t.rows_copied:<8} dest={t.dest_count_after}"
            )
        if not report.dry_run:
            chain_state = "OK" if report.audit_chain.ok else "BROKEN"
            typer.echo(f"audit chain: {chain_state}")

    if report.dry_run:
        return
    if report.ok:
        if not as_json:
            typer.secho(
                "Migration verified: row counts match and the audit chain is intact.",
                fg=typer.colors.GREEN,
            )
        return
    if not as_json:
        typer.secho(
            "Migration completed but verification found a problem — do not treat the "
            "destination as authoritative yet.",
            fg=typer.colors.RED,
            err=True,
        )
    raise typer.Exit(code=2)


__all__ = ["app"]
