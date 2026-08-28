"""Engine, pooling, statement-timeout, UTC handling, health check, clean shutdown, and
a real deadlock resolved via ``run_in_session_with_retry`` — all against a real,
pinned, loopback PostgreSQL instance (see ``conftest.py``).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select, text

from n8n_operator.storage.health import check_database_health
from n8n_operator.storage.models import Principal
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    run_in_session_with_retry,
    session_scope,
)

pytestmark = pytest.mark.postgres


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url)


class TestPooling:
    def test_pool_size_is_honored(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url, pool_size=2, max_overflow=1)
        try:
            assert engine.pool.size() == 2  # type: ignore[attr-defined]
        finally:
            engine.dispose()

    def test_pool_pre_ping_is_enabled(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url)
        try:
            assert engine.pool._pre_ping is True
        finally:
            engine.dispose()

    def test_dispose_closes_every_pooled_connection(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url)
        with engine.connect() as conn:
            conn.execute(select(1))
        assert engine.pool.checkedin() >= 0  # type: ignore[attr-defined]
        engine.dispose()
        assert engine.pool.checkedin() == 0  # type: ignore[attr-defined]


class TestStatementTimeout:
    def test_a_statement_exceeding_the_timeout_is_cancelled(
        self, postgres_test_db_url: str
    ) -> None:
        engine = create_engine_for_url(postgres_test_db_url, statement_timeout_seconds=1)
        try:
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError), engine.connect() as conn:
                conn.execute(text("SELECT pg_sleep(5)"))
        finally:
            engine.dispose()

    def test_a_fast_statement_is_unaffected(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url, statement_timeout_seconds=1)
        try:
            with engine.connect() as conn:
                result = conn.execute(select(1)).scalar_one()
            assert result == 1
        finally:
            engine.dispose()


class TestUTCHandling:
    def test_session_timezone_is_utc(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url)
        try:
            with engine.connect() as conn:
                tz = conn.execute(text("SHOW TIME ZONE")).scalar_one()
            assert tz.upper() == "UTC"
        finally:
            engine.dispose()

    def test_a_stored_timestamp_round_trips_as_utc_aware(self, postgres_test_db_url: str) -> None:
        engine = _migrated_engine(postgres_test_db_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                p = Principal(kind="local", display_name="local")
                session.add(p)
                session.flush()
                principal_id = p.id
            with session_scope(factory) as session:
                stored = session.get(Principal, principal_id)
                assert stored is not None
                assert stored.created_at.tzinfo is not None
                assert stored.created_at.utcoffset() == datetime.now(UTC).utcoffset()
        finally:
            engine.dispose()


class TestHealthCheck:
    def test_reachable_database_reports_healthy(self, postgres_test_db_url: str) -> None:
        engine = create_engine_for_url(postgres_test_db_url)
        try:
            health = check_database_health(engine)
            assert health.reachable is True
            assert health.dialect == "postgresql"
            assert health.latency_ms is not None
            assert health.error is None
        finally:
            engine.dispose()

    def test_unreachable_database_reports_unhealthy_without_a_credential_leak(self) -> None:
        engine = create_engine_for_url(
            "postgresql+psycopg://baduser:badpass@127.0.0.1:1/doesnotexist",
            connect_timeout_seconds=1,
        )
        try:
            health = check_database_health(engine)
            assert health.reachable is False
            assert health.error is not None
            assert "badpass" not in health.error
        finally:
            engine.dispose()


class TestRealDeadlockResolvedByRetry:
    def test_two_transactions_locking_two_rows_in_opposite_order_resolve_via_retry(
        self, postgres_test_db_url: str
    ) -> None:
        """The classic deadlock shape: transaction A locks row 1 then wants row 2;
        transaction B locks row 2 then wants row 1. PostgreSQL detects the cycle and
        kills one (SQLSTATE 40P01). ``run_in_session_with_retry`` must resolve the
        killed side without any caller-visible failure — proving the primitive against
        a real deadlock, not just a simulated error code."""
        engine = _migrated_engine(postgres_test_db_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                p1 = Principal(id="p1", kind="local", display_name="one")
                p2 = Principal(id="p2", kind="local", display_name="two")
                session.add_all([p1, p2])

            outcomes: dict[str, str] = {}

            def _lock_then_update(first: str, second: str, label: str) -> None:
                def txn(session: object) -> None:
                    from sqlalchemy import update

                    session.execute(  # type: ignore[attr-defined]
                        update(Principal)
                        .where(Principal.id == first)
                        .values(display_name=f"{label}-locked-{first}")
                    )
                    # Holds the first row's lock server-side, independent of any
                    # cross-thread synchronization primitive, while the other thread
                    # (running the same shape with rows reversed) gets a chance to grab
                    # its own first lock — this repeats identically on every retry, so
                    # a deadlock's eventual loser reliably meets the same collision
                    # again until PostgreSQL resolves the cycle by killing one side.
                    session.execute(text("SELECT pg_sleep(0.3)"))  # type: ignore[attr-defined]
                    session.execute(  # type: ignore[attr-defined]
                        update(Principal)
                        .where(Principal.id == second)
                        .values(display_name=f"{label}-locked-{second}")
                    )

                try:
                    run_in_session_with_retry(factory, txn, max_attempts=10, backoff_seconds=0.05)
                    outcomes[label] = "ok"
                except Exception as exc:  # pragma: no cover - only on a real failure
                    outcomes[label] = f"failed: {exc!r}"

            t1 = threading.Thread(target=_lock_then_update, args=("p1", "p2", "A"))
            t2 = threading.Thread(target=_lock_then_update, args=("p2", "p1", "B"))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

            assert outcomes == {"A": "ok", "B": "ok"}
        finally:
            engine.dispose()
