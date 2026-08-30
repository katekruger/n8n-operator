"""Fixtures for the pinned, loopback-only PostgreSQL integration harness.

Every test module under this package is marked ``postgres`` (BUILD_PLAN section 10.1)
and skips unless ``N8N_OPERATOR_TEST_POSTGRES_URL`` is set — a base/maintenance
connection URL (typically pointing at Postgres's own ``postgres`` database) with
permission to ``CREATE DATABASE``/``DROP DATABASE``. CI provides this via a pinned
``postgres:16`` service container bound to loopback; a local run provides it the same
way ``N8N_LIVE_*`` provides a real n8n instance for ``tests/live/`` — opt-in, not a
default part of the suite.

Each test gets its own freshly created, empty PostgreSQL database, dropped again on
teardown — full isolation per test, not a shared schema every test has to clean up
after itself.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from n8n_operator.storage.session import create_engine_for_url

pytestmark = pytest.mark.postgres


def _base_url() -> str:
    url = os.environ.get("N8N_OPERATOR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("N8N_OPERATOR_TEST_POSTGRES_URL is required for postgres tests")
    return url


@pytest.fixture
def postgres_test_db_url() -> Iterator[str]:
    """A fresh, empty PostgreSQL database's URL. Created before the test, dropped after."""
    base = _base_url()
    db_name = f"n8n_operator_test_{uuid.uuid4().hex[:16]}"

    admin_engine = create_engine_for_url(base)
    admin_engine = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()

    test_url = make_url(base).set(database=db_name).render_as_string(hide_password=False)
    try:
        yield test_url
    finally:
        admin_engine = create_engine_for_url(base)
        admin_engine = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as conn:
                # Terminate any connections this test left open (a leaked engine that
                # was never disposed) — DROP DATABASE refuses while any exist.
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": db_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            admin_engine.dispose()
