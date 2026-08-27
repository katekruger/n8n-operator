"""``n8n-operator db`` — init, migrate, status.

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
from n8n_operator.config import resolve_database_url
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

    typer.echo(f"database_url:     {database_url}")
    typer.echo(f"current revision: {current_revision or '(none — not initialized)'}")
    typer.echo(f"head revision:    {head_revision}")

    if current_revision == head_revision:
        typer.echo("status: up to date")
        return
    if current_revision is None:
        typer.echo("status: not initialized — run `n8n-operator db init`")
        raise typer.Exit(code=1)
    typer.echo("status: behind head — run `n8n-operator db migrate`")
    raise typer.Exit(code=1)


__all__ = ["app"]
