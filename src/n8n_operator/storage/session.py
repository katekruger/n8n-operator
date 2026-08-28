"""Engine and session lifecycle.

SQLite runs in WAL mode with a busy timeout and foreign-key enforcement turned on, all
configured at connection setup only so none of it leaks into the schema (ADR-004 rule
D9). ``PRAGMA foreign_keys=ON`` is not the default in SQLite and must be set on every
connection; without it, every foreign key declared in ``storage/models.py`` is silently
decorative.

PostgreSQL (v2, ADR-004) gets a production-safe connection pool (bounded size, overflow,
pre-ping, recycle), a per-connection statement timeout, and an explicit UTC session
timezone — all configured at connection/engine setup only, for the same reason the
SQLite pragmas are: none of it is a schema concern.

:func:`session_scope` is the one sanctioned way to open a transaction outside Alembic:
it commits on a clean exit and rolls back on any exception, so a partial write — half an
operation update, no matching event row — can never be observed by another reader
(invariant I6, ARCHITECTURE section 6.1).

:func:`run_in_session_with_retry` is a separate, narrower primitive for DB-only work
that is safe to retry on a transient Postgres serialization failure or deadlock (or
SQLite lock contention). It is deliberately not used by ``core/service.py``'s existing
``prepare_operation``/``execute_operation`` call sites in this phase — automatic retry
of security-critical CAS logic is a larger behavioral change than "add Postgres
support," and ADR-005's no-automatic-retry discipline is exactly the kind of thing that
should not acquire a new exception under time pressure. It is used by
``storage/postgres_migration.py``'s row-copy loop, where retrying a failed chunk is
unambiguously safe (no external side effect has occurred), and is available for a
future stage to adopt deliberately, with its own review, rather than by accident here.

Phase 1 (BUILD_PLAN section 12); PostgreSQL support and the retry primitive added in
phase 10 (v2) stage 01.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from sqlite3 import Connection as SQLite3Connection
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import (
    DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_RECYCLE_SECONDS,
    DEFAULT_DATABASE_POOL_SIZE,
    DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_STATEMENT_TIMEOUT_SECONDS,
)

# Postgres SQLSTATEs worth retrying a DB-only transaction against: serialization_failure
# (only reachable under SERIALIZABLE isolation, which this codebase does not use, but
# checked anyway as a defensive no-op) and deadlock_detected (reachable under the
# default READ COMMITTED isolation whenever two transactions acquire row locks in
# different orders — a real possibility once Postgres allows concurrent writers).
_RETRYABLE_POSTGRES_SQLSTATES = frozenset({"40001", "40P01"})


def create_engine_for_url(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = DEFAULT_DATABASE_POOL_SIZE,
    max_overflow: int = DEFAULT_DATABASE_MAX_OVERFLOW,
    pool_timeout: int = DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS,
    pool_recycle: int = DEFAULT_DATABASE_POOL_RECYCLE_SECONDS,
    statement_timeout_seconds: int = DEFAULT_DATABASE_STATEMENT_TIMEOUT_SECONDS,
    connect_timeout_seconds: int = DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS,
) -> Engine:
    """Build a SQLAlchemy engine, dialect-appropriate.

    On SQLite: a plain engine plus the WAL/busy-timeout/foreign-key pragmas (ADR-004
    D9); the pool arguments are meaningless for a local file database and are ignored.

    On PostgreSQL: a bounded ``QueuePool`` (``pool_size`` steady connections,
    ``max_overflow`` beyond that under load, ``pool_timeout`` before a caller waiting
    for a connection gives up), ``pool_pre_ping=True`` (a stale connection — e.g. one a
    managed database silently dropped while idle — is detected and replaced before a
    caller ever sees it fail), ``pool_recycle`` (connections older than this are
    recycled even if they still look healthy, ahead of most providers' own idle-kill
    windows), a libpq ``connect_timeout``, a per-connection ``statement_timeout`` (a
    single runaway statement cannot hang a worker indefinitely — Operator's own queries
    are all simple, indexed lookups; a query that needs longer than this is a bug, not
    a workload), and an explicit ``SET TIME ZONE 'UTC'`` on every new connection (defense
    in depth alongside :class:`~n8n_operator.storage.models.UTCDateTime` — nothing in
    this codebase issues a server-side ``NOW()``, but a session that is unambiguously
    UTC end to end has one fewer thing to get wrong).

    The dialect is read from the parsed URL, not from the not-yet-created engine, since
    the pool arguments are constructor-time-only on ``create_engine``.
    """
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.engine import make_url

    backend = make_url(database_url).get_backend_name()
    kwargs: dict[str, Any] = {"echo": echo, "future": True}

    if backend == "postgresql":
        kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,
            connect_args={"connect_timeout": connect_timeout_seconds},
        )

    engine = _create_engine(database_url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            if not isinstance(dbapi_connection, SQLite3Connection):
                return  # pragma: no cover - defensive; the sqlite3 DBAPI always matches
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    elif engine.dialect.name == "postgresql":
        statement_timeout_ms = statement_timeout_seconds * 1000

        @event.listens_for(engine, "connect")
        def _set_postgres_session(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # Both are session-scoped GUCs, not part of the schema (ADR-004 D9's
                # reasoning extended to Postgres): set once per new physical connection,
                # never varied per statement or transaction.
                cursor.execute(f"SET statement_timeout = {statement_timeout_ms}")
                cursor.execute("SET TIME ZONE 'UTC'")
            finally:
                cursor.close()
            # psycopg's default autocommit=False means the two SET statements above
            # left the physical connection with an open transaction (INTRANS). Commit
            # it here so the connection is handed back to SQLAlchemy idle — otherwise a
            # caller requesting AUTOCOMMIT isolation (e.g. DDL like CREATE DATABASE)
            # fails outright: psycopg refuses to toggle autocommit mid-transaction.
            dbapi_connection.commit()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to ``engine``. Callers open transactions via
    :func:`session_scope`, not by calling this factory's result directly."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction per context.

    Commits on a clean exit; rolls back and re-raises on any exception. This is what
    makes "atomic operation/event/audit writes" actually atomic — the repository
    functions in ``storage/repository.py`` take a ``Session`` and do not manage their own
    transaction boundary, so a caller can compose several of them (an operation update,
    an event insert, an audit insert) inside one call to this context manager and get one
    commit or none at all.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _is_retryable_transaction_error(exc: BaseException) -> bool:
    """A transient failure a fresh attempt of the *same, DB-only* work would plausibly
    resolve — never a data problem (a constraint violation is never retryable: retrying
    it produces the identical violation)."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in _RETRYABLE_POSTGRES_SQLSTATES:
        return True
    # SQLite's own transient-contention signal. A busy_timeout is already configured
    # (D9), so this only fires once that timeout itself has been exhausted — a real,
    # if rare, possibility under write-heavy WAL contention.
    return "database is locked" in str(orig).lower()


def run_in_session_with_retry[T](
    session_factory: sessionmaker[Session],
    fn: Callable[[Session], T],
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 0.05,
) -> T:
    """Run ``fn(session)`` inside its own transaction, retrying with a fresh session and
    transaction on a transient serialization/deadlock failure.

    ``fn`` must perform **only** database work. This function's safety rests entirely on
    that: a retry re-executes ``fn`` from the top, so anything inside it with an
    observable effect outside the database — dispatching to n8n, sending a notification —
    would be repeated too, exactly the outcome ADR-005 forbids. This is a plain function
    taking a callable, not a context manager, precisely so it cannot be handed a `with`
    block that half-executed once already: retrying the *body* of an already-entered
    context is not something a context manager can safely do, since the caller's code
    between `yield` points has already run and cannot be rewound.

    A non-retryable exception (anything ``session.rollback()`` doesn't turn into a clean
    retry candidate — a constraint violation, an application-raised error, a permanent
    connection failure) propagates immediately, on the first attempt, same as
    :func:`session_scope`.
    """
    attempt = 0
    while True:
        attempt += 1
        session = session_factory()
        try:
            result = fn(session)
            session.commit()
            return result
        except BaseException as exc:
            session.rollback()
            if attempt < max_attempts and _is_retryable_transaction_error(exc):
                time.sleep(backoff_seconds * attempt)
                continue
            raise
        finally:
            session.close()


__all__ = [
    "create_engine_for_url",
    "create_session_factory",
    "run_in_session_with_retry",
    "session_scope",
]
