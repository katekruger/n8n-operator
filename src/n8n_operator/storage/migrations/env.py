"""Alembic migration environment.

The schema is created by migrations, never by ``create_all`` outside tests (ADR-004 rule
D6). AC-24 requires that autogenerate against the ORM metadata produce an empty diff, so
schema and models cannot silently diverge — this file wires ``target_metadata`` to the
same ``Base.metadata`` in ``storage/models.py`` that the rest of the application uses,
rather than a second, hand-maintained copy.

The database URL is resolved via :func:`n8n_operator.config.resolve_database_url`, not
by constructing the full :class:`~n8n_operator.config.Settings` — that would require
``N8N_BASE_URL``/``N8N_API_KEY`` to be set, an orthogonal concern a bare
``alembic upgrade head`` should not need to satisfy just to manage the schema.
Resolution order:

1. ``-x db_url=...`` passed to the ``alembic`` command (``alembic -x db_url=... upgrade head``)
2. ``Config.get_main_option("sqlalchemy.url")`` — what a programmatic caller (the ``db``
   CLI commands, and this project's own migration tests) sets via
   ``Config.set_main_option("sqlalchemy.url", ...)`` before calling
   ``alembic.command.upgrade``. This is the standard Alembic channel for driving
   migrations from Python rather than the command line, and is what lets a caller target
   an arbitrary database — a test's ``tmp_path`` fixture, in particular — without that
   URL having to also be exported as an environment variable.
3. the ``N8N_OPERATOR_DATABASE_URL`` environment variable
4. :data:`n8n_operator.config.DEFAULT_DATABASE_URL`

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from n8n_operator.config import resolve_database_url
from n8n_operator.storage.models import Base

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    explicit = x_args.get("db_url") or context.config.get_main_option("sqlalchemy.url")
    return resolve_database_url(explicit)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection ("alembic upgrade --sql")."""
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection — the normal path."""
    configuration = context.config.get_section(context.config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
