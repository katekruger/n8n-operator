"""Migration round-trip, autogenerate-empty, and upgrade-from-empty (AC-24).

Drives Alembic's own ``alembic.command`` API against a real, file-based SQLite database
under pytest's ``tmp_path`` — the same programmatic path ``cli/commands/db.py`` uses, so
these tests exercise exactly the mechanism the CLI relies on, not a parallel one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from n8n_operator.storage.models import Base

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "n8n_operator" / "storage" / "migrations"
)


def _alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture
def migration_db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}"


@pytest.mark.integration
def test_empty_database_upgrades_to_head(migration_db_url: str) -> None:
    """AC-24: ``n8n-operator db migrate`` brings an empty database to head."""
    cfg = _alembic_config(migration_db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(migration_db_url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert current == "0006"


@pytest.mark.integration
def test_upgrade_creates_every_table(migration_db_url: str) -> None:
    cfg = _alembic_config(migration_db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(migration_db_url)
    try:
        from sqlalchemy import inspect

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables.keys()) | {"alembic_version"}
    assert tables == expected


@pytest.mark.integration
def test_downgrade_then_upgrade_round_trips(migration_db_url: str) -> None:
    cfg = _alembic_config(migration_db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(migration_db_url)
    try:
        from sqlalchemy import inspect

        inspector = inspect(engine)
        tables_after_downgrade = set(inspector.get_table_names())
    finally:
        engine.dispose()
    # alembic_version itself survives a downgrade to "base" (it just records no version).
    assert tables_after_downgrade <= {"alembic_version"}

    command.upgrade(cfg, "head")
    engine = create_engine(migration_db_url)
    try:
        from sqlalchemy import inspect

        inspector = inspect(engine)
        tables_after_reupgrade = set(inspector.get_table_names())
    finally:
        engine.dispose()
    assert tables_after_reupgrade == set(Base.metadata.tables.keys()) | {"alembic_version"}


@pytest.mark.integration
def test_autogenerate_against_head_produces_an_empty_diff(migration_db_url: str) -> None:
    """AC-24's exact wording: "the resulting schema matches the ORM metadata
    (autogenerate produces an empty diff)". Uses Alembic's own ``compare_metadata`` —
    the same comparison ``alembic revision --autogenerate`` performs — rather than the
    CLI's textual "No new upgrade operations detected." message, so this assertion is
    against a structured result, not a string a future Alembic version could reword."""
    cfg = _alembic_config(migration_db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(migration_db_url)
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(connection)
            diff = compare_metadata(migration_context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"schema and ORM metadata disagree: {diff}"


@pytest.mark.integration
def test_migration_is_idempotent_at_head(migration_db_url: str) -> None:
    """Upgrading twice in a row (already at head) is a no-op, not an error."""
    cfg = _alembic_config(migration_db_url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise

    engine = create_engine(migration_db_url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert current == "0006"


@pytest.mark.integration
def test_head_revision_is_0006() -> None:
    from alembic.script import ScriptDirectory

    cfg = _alembic_config("sqlite+pysqlite:///:memory:")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "0006"
