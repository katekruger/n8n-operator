"""Engine and session lifecycle.

SQLite runs in WAL mode with a busy timeout and foreign-key enforcement turned on, all
configured at connection setup only so none of it leaks into the schema (ADR-004 rule
D9). ``PRAGMA foreign_keys=ON`` is not the default in SQLite and must be set on every
connection; without it, every foreign key declared in ``storage/models.py`` is silently
decorative.

:func:`session_scope` is the one sanctioned way to open a transaction outside Alembic:
it commits on a clean exit and rolls back on any exception, so a partial write — half an
operation update, no matching event row — can never be observed by another reader
(invariant I6, ARCHITECTURE section 6.1).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from sqlite3 import Connection as SQLite3Connection
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker


def create_engine_for_url(database_url: str, *, echo: bool = False) -> Engine:
    """Build a SQLAlchemy engine, wiring the SQLite connection pragmas ADR-004 D9 needs.

    On any other dialect (PostgreSQL in v2) this is a plain ``create_engine`` — the
    pragmas below are meaningless outside SQLite and are only installed when the engine
    is actually SQLite, checked by dialect name rather than by inspecting the URL string.
    """
    from sqlalchemy import create_engine as _create_engine

    engine = _create_engine(database_url, echo=echo, future=True)

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


__all__ = ["create_engine_for_url", "create_session_factory", "session_scope"]
