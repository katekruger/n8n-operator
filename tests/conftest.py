"""Shared pytest fixtures.

Test layers and their scope are defined in BUILD_PLAN section 10.1. Fixtures for the
mock n8n transport and a loaded registry snapshot arrive with the phases that need them;
the SQLite database fixtures below land with phase 1.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.storage.models import Base
from n8n_operator.storage.session import create_engine_for_url, create_session_factory


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    """A file-based SQLite URL under pytest's per-test temp directory.

    Deliberately not ``:memory:``: WAL mode and cross-connection visibility both need a
    real file, and the FK/portability behavior this phase tests should be exercised
    against the same kind of database the application actually runs on.
    """
    return f"sqlite+pysqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(sqlite_url: str) -> Iterator[Engine]:
    eng = create_engine_for_url(sqlite_url)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def seed(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """A minimal, valid row in every table an ``Operation`` foreign-keys to.

    Most repository and constraint tests need a principal and a registry snapshot to
    exist before an operation can be inserted at all; this fixture creates exactly those
    and returns their IDs, so individual tests can focus on what they are actually
    testing rather than re-deriving this setup every time.
    """
    from n8n_operator.storage.repository import PrincipalRepository, RegistrySnapshotRepository
    from n8n_operator.storage.session import session_scope

    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(kind="local", display_name="local")
        snapshot = RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "a" * 64,
            source_path="./workflows.yaml",
            document={},
        )
        return {"principal_id": principal.id, "snapshot_id": snapshot.id}
