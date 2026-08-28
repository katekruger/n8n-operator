"""SQLite -> PostgreSQL migration, against a real, pinned, loopback PostgreSQL instance.

Covers the stage-01 required edge cases: an empty database, a fully populated one
(every operation state, approvals, a result, a snapshot, and a real audit chain),
duplicate/conflicting rows, partial-copy interruption and resumption, and
destination-not-empty refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from n8n_operator.core.postgres_migration import migrate
from n8n_operator.storage.models import STATES, Base
from n8n_operator.storage.postgres_migration import (
    MigrationRefusedError,
    preflight,
)
from n8n_operator.storage.session import create_engine_for_url, create_session_factory

from ._seed import seed_full_v1_fixture

pytestmark = pytest.mark.postgres


@pytest.fixture
def sqlite_source_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'source.db'}"


def _row_counts(url: str) -> dict[str, int]:
    engine = create_engine_for_url(url)
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            return {
                name: session.execute(select(func.count()).select_from(table)).scalar_one()
                for name, table in Base.metadata.tables.items()
            }
    finally:
        engine.dispose()


class TestEmptyDatabase:
    def test_migrating_an_empty_sqlite_database_succeeds_and_verifies(
        self, sqlite_source_url: str, postgres_test_db_url: str
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config

        command.upgrade(_alembic_config(sqlite_source_url), "head")

        report = migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)

        assert report.ok
        assert all(t.source_count == 0 for t in report.tables)
        assert report.audit_chain is not None
        assert report.audit_chain.ok


class TestPopulatedDatabase:
    @pytest.fixture
    def populated_sqlite_url(self, sqlite_source_url: str) -> tuple[str, dict[str, Any]]:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            ids = seed_full_v1_fixture(factory)
        finally:
            engine.dispose()
        return sqlite_source_url, ids

    def test_every_state_approval_result_and_snapshot_migrates(
        self, populated_sqlite_url: tuple[str, dict[str, Any]], postgres_test_db_url: str
    ) -> None:
        source_url, _ids = populated_sqlite_url
        source_counts = _row_counts(source_url)
        assert source_counts["operations"] == len(STATES)
        assert source_counts["approvals"] == 1
        assert source_counts["execution_results"] == 1
        assert source_counts["audit_log"] == len(STATES) + 1  # one per op + one registry reload

        report = migrate(source_url=source_url, dest_url=postgres_test_db_url)

        assert report.ok, report
        for table_result in report.tables:
            assert table_result.dest_count_after == table_result.source_count, table_result

        dest_counts = _row_counts(postgres_test_db_url)
        assert dest_counts == source_counts

    def test_audit_chain_reverifies_intact_on_the_destination(
        self, populated_sqlite_url: tuple[str, dict[str, Any]], postgres_test_db_url: str
    ) -> None:
        source_url, _ids = populated_sqlite_url
        report = migrate(source_url=source_url, dest_url=postgres_test_db_url)
        assert report.audit_chain is not None
        assert report.audit_chain.ok
        assert report.audit_chain.first_break_seq is None

    def test_source_database_is_never_written_to(
        self, populated_sqlite_url: tuple[str, dict[str, Any]], postgres_test_db_url: str
    ) -> None:
        source_url, _ids = populated_sqlite_url
        before = _row_counts(source_url)
        migrate(source_url=source_url, dest_url=postgres_test_db_url)
        after = _row_counts(source_url)
        assert before == after

    def test_unicode_and_json_payloads_survive_the_copy_byte_for_byte(
        self, populated_sqlite_url: tuple[str, dict[str, Any]], postgres_test_db_url: str
    ) -> None:
        source_url, ids = populated_sqlite_url
        migrate(source_url=source_url, dest_url=postgres_test_db_url)

        engine = create_engine_for_url(postgres_test_db_url)
        try:
            factory = create_session_factory(engine)
            with factory() as session:
                from n8n_operator.storage.repository import OperationRepository

                op = OperationRepository(session).get(ids["operation_ids"]["PREPARING"])
                assert op is not None
                assert op.arguments["unicode"] == "café ☃ — em dash"
        finally:
            engine.dispose()


class TestDestinationNotEmptyRefusal:
    def test_refuses_a_destination_already_holding_rows(
        self, sqlite_source_url: str, postgres_test_db_url: str
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            seed_full_v1_fixture(factory)
        finally:
            engine.dispose()

        # First migration succeeds and populates the destination.
        migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)

        # A second, independent migration attempt against the now-populated
        # destination — no checkpoint for *this* run — must refuse outright.
        with pytest.raises(MigrationRefusedError, match="already has"):
            migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)


class TestDuplicateSourceRows:
    def test_a_source_row_colliding_with_an_existing_destination_row_fails_closed(
        self, sqlite_source_url: str, postgres_test_db_url: str
    ) -> None:
        """Exercises ``_copy_table`` directly against a destination that already holds a
        row with the same primary key the source is about to copy — the exact shape of
        "a duplicate source row" surviving past the initial not-empty check (e.g. a
        checkpoint that under-reports what was already copied). The copy must stop with
        a clear, typed error, never silently skip or overwrite the conflicting row."""
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config
        from n8n_operator.storage.postgres_migration import (
            _copy_table,
            _ensure_destination_schema_at_head,
        )
        from n8n_operator.storage.repository import PrincipalRepository
        from n8n_operator.storage.session import session_scope

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                PrincipalRepository(session).create(
                    id="p_conflict", kind="local", display_name="local"
                )
        finally:
            engine.dispose()

        _ensure_destination_schema_at_head(postgres_test_db_url)
        dest_engine = create_engine_for_url(postgres_test_db_url)
        try:
            dest_factory = create_session_factory(dest_engine)
            with session_scope(dest_factory) as session:
                PrincipalRepository(session).create(
                    id="p_conflict", kind="local", display_name="local"
                )

            with pytest.raises(MigrationRefusedError, match="conflicted"):
                _copy_table(
                    source_url=sqlite_source_url,
                    dest_factory=dest_factory,
                    table=Base.metadata.tables["principals"],
                    chunk_size=500,
                    already_copied=0,
                )
        finally:
            dest_engine.dispose()


class TestPreflightAndDryRun:
    def test_preflight_reports_source_counts_without_writing(
        self, sqlite_source_url: str, postgres_test_db_url: str
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            seed_full_v1_fixture(factory)
        finally:
            engine.dispose()

        counts = preflight(sqlite_source_url, postgres_test_db_url)
        assert counts["operations"] == len(STATES)

        # preflight must not have touched the destination at all.
        from sqlalchemy import inspect

        dest_engine = create_engine_for_url(postgres_test_db_url)
        try:
            with dest_engine.connect() as conn:
                assert inspect(conn).get_table_names() == []
        finally:
            dest_engine.dispose()

    def test_dry_run_reports_the_plan_and_writes_nothing(
        self, sqlite_source_url: str, postgres_test_db_url: str
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            seed_full_v1_fixture(factory)
        finally:
            engine.dispose()

        report = migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url, dry_run=True)
        assert report.dry_run is True
        table_by_name = {t.table_name: t for t in report.tables}
        assert table_by_name["operations"].source_count == len(STATES)
        assert table_by_name["operations"].rows_copied == 0

        from sqlalchemy import inspect

        dest_engine = create_engine_for_url(postgres_test_db_url)
        try:
            with dest_engine.connect() as conn:
                assert inspect(conn).get_table_names() == []
        finally:
            dest_engine.dispose()

    def test_source_must_be_sqlite_and_destination_must_be_postgresql(
        self, postgres_test_db_url: str
    ) -> None:
        with pytest.raises(MigrationRefusedError, match="source must be a SQLite URL"):
            preflight(postgres_test_db_url, postgres_test_db_url)


class TestResumption:
    def test_interrupted_copy_resumes_from_the_checkpoint(
        self, sqlite_source_url: str, postgres_test_db_url: str, tmp_path: Path
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config
        from n8n_operator.storage.postgres_migration import (
            _ensure_destination_schema_at_head,
            _write_checkpoint,
        )

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            seed_full_v1_fixture(factory)
        finally:
            engine.dispose()

        source_counts = preflight(sqlite_source_url, postgres_test_db_url)
        checkpoint_path = tmp_path / "checkpoint.json"

        # Simulate a crash after "principals" and "registry_snapshots" (both
        # dependency-free, so always early in copy order) had already committed, by
        # hand-writing a checkpoint claiming exactly that.
        _ensure_destination_schema_at_head(postgres_test_db_url)
        dest_engine = create_engine_for_url(postgres_test_db_url)
        try:
            dest_factory = create_session_factory(dest_engine)
            from n8n_operator.storage.postgres_migration import _copy_table, _ordered_tables

            completed = {}
            for table in _ordered_tables():
                if table.name in ("principals", "registry_snapshots"):
                    rows_copied = _copy_table(
                        source_url=sqlite_source_url,
                        dest_factory=dest_factory,
                        table=table,
                        chunk_size=500,
                        already_copied=0,
                    )
                    completed[table.name] = rows_copied
        finally:
            dest_engine.dispose()

        _write_checkpoint(
            checkpoint_path,
            {"format_version": 1, "source_counts": source_counts, "completed_tables": completed},
        )

        report = migrate(
            source_url=sqlite_source_url,
            dest_url=postgres_test_db_url,
            checkpoint_path=checkpoint_path,
            resume=True,
        )

        assert report.resumed is True
        assert report.ok
        dest_counts = _row_counts(postgres_test_db_url)
        assert dest_counts["principals"] == source_counts["principals"]
        assert dest_counts["operations"] == source_counts["operations"]
        # The checkpoint is cleaned up once the resumed run finishes and verifies.
        assert not checkpoint_path.exists()

    def test_resume_without_a_checkpoint_flag_refuses_when_one_exists(
        self, sqlite_source_url: str, postgres_test_db_url: str, tmp_path: Path
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config
        from n8n_operator.storage.postgres_migration import _write_checkpoint

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        checkpoint_path = tmp_path / "checkpoint.json"
        _write_checkpoint(
            checkpoint_path,
            {"format_version": 1, "source_counts": {}, "completed_tables": {}},
        )

        with pytest.raises(MigrationRefusedError, match="checkpoint already exists"):
            migrate(
                source_url=sqlite_source_url,
                dest_url=postgres_test_db_url,
                checkpoint_path=checkpoint_path,
                resume=False,
            )

    def test_resume_against_a_changed_source_is_refused(
        self, sqlite_source_url: str, postgres_test_db_url: str, tmp_path: Path
    ) -> None:
        from alembic import command

        from n8n_operator.cli.commands.db import _alembic_config
        from n8n_operator.storage.postgres_migration import _write_checkpoint
        from n8n_operator.storage.repository import PrincipalRepository
        from n8n_operator.storage.session import session_scope

        command.upgrade(_alembic_config(sqlite_source_url), "head")
        checkpoint_path = tmp_path / "checkpoint.json"
        _write_checkpoint(
            checkpoint_path,
            {
                "format_version": 1,
                "source_counts": {"principals": 999},  # stale — does not match reality
                "completed_tables": {},
            },
        )

        engine = create_engine_for_url(sqlite_source_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                PrincipalRepository(session).create(kind="local", display_name="local")
        finally:
            engine.dispose()

        with pytest.raises(MigrationRefusedError, match="source changed"):
            migrate(
                source_url=sqlite_source_url,
                dest_url=postgres_test_db_url,
                checkpoint_path=checkpoint_path,
                resume=True,
            )
