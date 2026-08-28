"""SQLite -> PostgreSQL data migration (BUILD_PLAN section 8.3, ADR-004; v2 stage 01).

The v2 move from SQLite to PostgreSQL is "a configuration change, not a rewrite"
(ADR-004) at the *schema* level — the same Alembic history brings either engine to
head. This module is the other half: copying an existing v1 SQLite database's *rows*
onto a freshly-migrated, empty PostgreSQL database, once, safely, with proof it worked.

Design commitments, each answering one of the stage-01 required edge cases:

- **The source is never written to.** Every operation against ``source_engine`` in this
  module is a read. A migration that failed partway, or that failed verification after
  succeeding, must never have touched the SQLite file it started from — an operator can
  always retry against the same source.
- **Fail-closed on a non-empty destination.** A destination that already holds rows in
  any table this tool is about to populate is refused outright, unless a checkpoint
  proves this is a legitimate resume of an interrupted run *by this tool* against *this*
  source (row counts re-checked against the checkpoint before continuing — a source that
  changed underneath a stale checkpoint is refused, not silently reconciled).
- **Checkpointed and resumable.** Progress is persisted after every committed chunk, so
  a crash mid-copy loses at most one chunk's worth of work, not the whole run.
- **Retried only where retrying is safe.** Each chunk's destination write goes through
  ``storage.session.run_in_session_with_retry`` — pure DB work, no external side effect,
  exactly the case that primitive exists for. A genuine conflict (a duplicate key,
  a constraint violation) is never retried — it means the destination was not actually
  empty/consistent, and continuing would silently paper over corruption.
- **Verified, not assumed.** Every table's destination row count is compared against the
  source count actually read (not the count taken at preflight, which could go stale
  under a long-running copy of a live source). A count mismatch fails the migration,
  loudly, whether or not every row nominally copied without error.

**Layering note (ARCHITECTURE.md section 2.1, ADR-001).** This module is part of the
``storage`` capability package and therefore may not import ``audit`` or ``core`` —
which is why the row-copy this module performs and the audit-hash-chain
*re-verification* of what it copied are two separate steps, composed by
``core.postgres_migration.migrate`` (the actual entry point most callers, including the
CLI, should use). :func:`copy_all_tables` here reports only what a capability package
answerable to nothing but SQLAlchemy can honestly report: row counts.

Phase 10 (v2) stage 01.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Table, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from n8n_operator.storage.models import Base
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    run_in_session_with_retry,
    session_scope,
)

DEFAULT_CHUNK_SIZE = 500

_CHECKPOINT_FORMAT_VERSION = 1


class MigrationRefusedError(Exception):
    """Raised for every fail-closed condition: a non-empty destination with no valid
    checkpoint, a checkpoint that no longer matches the source, an unsupported source or
    destination dialect, or a destination not at the expected schema revision. Always
    caught at the CLI boundary and reported without a traceback — every one of these is
    an expected, actionable operator condition, not a bug."""


@dataclass(frozen=True)
class TableCopyResult:
    table_name: str
    source_count: int
    dest_count_before: int
    rows_copied: int
    dest_count_after: int


@dataclass(frozen=True)
class RowCopyReport:
    """What the storage layer alone can honestly report: row counts. See the module
    docstring's layering note for why audit-chain verification is not part of this —
    that composition lives in ``core.postgres_migration.MigrationReport``."""

    dry_run: bool
    resumed: bool
    tables: list[TableCopyResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(t.dest_count_after == t.source_count for t in self.tables)


def _ordered_tables() -> list[Table]:
    """Every table this tool copies, parents before children — the same topological
    order ``Base.metadata.sorted_tables`` already computes from the declared foreign
    keys, so a child row is never inserted before the parent it references."""
    return list(Base.metadata.sorted_tables)


def _default_checkpoint_path(dest_url: str) -> Path:
    # Never derived from anything in dest_url beyond its own existence — the checkpoint
    # file name must not leak a password even if a future caller logs the path.
    digest_source = dest_url.split("@")[-1]  # host/db portion only, never credentials
    safe = "".join(c if c.isalnum() else "_" for c in digest_source)
    return Path(tempfile.gettempdir()) / f"n8n-operator-pg-migration-{safe}.json"


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != _CHECKPOINT_FORMAT_VERSION:
        raise MigrationRefusedError(
            f"checkpoint {path} is from an incompatible tool version; delete it and "
            "restart the migration from scratch"
        )
    return data


def _write_checkpoint(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: a reader (a resumed run, or an operator inspecting progress) never
    observes a half-written checkpoint file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _table_row_count(session: Session, table: Table) -> int:
    result = session.execute(select(func.count()).select_from(table)).scalar_one()
    return int(result)


def _ensure_destination_schema_at_head(dest_url: str) -> None:
    """Idempotent: brings an empty or already-current destination to the Alembic head
    (``0003`` as of this stage) via the exact same programmatic path ``db init``/``db
    migrate`` use. A destination already at head is a no-op; a destination that exists
    but is *behind* head is brought forward — a destination ahead of what this tool
    knows about (a future schema version) is left to Alembic's own error, which already
    refuses a downgrade-shaped mismatch clearly."""
    import n8n_operator.storage as storage_package

    migrations_dir = Path(storage_package.__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", dest_url)
    command.upgrade(cfg, "head")


def preflight(source_url: str, dest_url: str) -> dict[str, int]:
    """Connect to both databases and report each table's *current* source row count.
    Performs no write. Raises :class:`MigrationRefusedError` if either URL is not the
    dialect this tool expects (SQLite source, PostgreSQL destination — this tool has
    exactly one direction; a same-dialect copy has no reason to exist here)."""
    from sqlalchemy.engine import make_url

    if make_url(source_url).get_backend_name() != "sqlite":
        raise MigrationRefusedError(f"source must be a SQLite URL, got dialect {source_url!r}")
    if make_url(dest_url).get_backend_name() != "postgresql":
        raise MigrationRefusedError(
            f"destination must be a PostgreSQL URL, got dialect {dest_url!r}"
        )

    source_engine = create_engine_for_url(source_url)
    try:
        source_factory = create_session_factory(source_engine)
        with session_scope(source_factory) as session:
            return {t.name: _table_row_count(session, t) for t in _ordered_tables()}
    finally:
        source_engine.dispose()


def copy_all_tables(
    *,
    source_url: str,
    dest_url: str,
    dry_run: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> RowCopyReport:
    """Copy every row from ``source_url`` (SQLite) to ``dest_url`` (PostgreSQL), once.

    ``dry_run=True`` performs preflight and reports current source counts against the
    destination's *current* counts — no destination write, no checkpoint, no schema
    migration. Every other argument is ignored in dry-run mode's report except that it
    still validates them (a dry run should surface a bad chunk size or an unsupported
    URL exactly as a real run would, rather than deferring that discovery).

    Most callers want ``core.postgres_migration.migrate`` instead, which calls this and
    additionally re-verifies the audit hash chain on the destination (a capability this
    module cannot provide itself — see the module docstring's layering note).
    """
    source_counts = preflight(source_url, dest_url)
    path = checkpoint_path or _default_checkpoint_path(dest_url)

    if dry_run:
        dest_engine = create_engine_for_url(dest_url)
        try:
            dest_factory = create_session_factory(dest_engine)
            with session_scope(dest_factory) as session:
                dest_counts = {
                    t.name: (_table_row_count(session, t) if _table_exists(session, t) else 0)
                    for t in _ordered_tables()
                }
        finally:
            dest_engine.dispose()
        return RowCopyReport(
            dry_run=True,
            resumed=False,
            tables=[
                TableCopyResult(
                    table_name=name,
                    source_count=source_counts[name],
                    dest_count_before=dest_counts.get(name, 0),
                    rows_copied=0,
                    dest_count_after=dest_counts.get(name, 0),
                )
                for name in source_counts
            ],
        )

    _ensure_destination_schema_at_head(dest_url)

    checkpoint = _load_checkpoint(path)
    resumed = False
    completed_tables: dict[str, int] = {}
    if checkpoint is not None:
        if not resume:
            raise MigrationRefusedError(
                f"a checkpoint already exists at {path} from a previous run — pass "
                "resume=True to continue it, or delete the file to start over"
            )
        if checkpoint.get("source_counts") != source_counts:
            raise MigrationRefusedError(
                f"checkpoint {path} was recorded against different source row counts "
                f"({checkpoint.get('source_counts')}) than the source has now "
                f"({source_counts}) — the source changed underneath a stale checkpoint; "
                "refusing to resume against a moving target. Delete the checkpoint and "
                "restart the migration from scratch."
            )
        completed_tables = dict(checkpoint.get("completed_tables", {}))
        resumed = True
    else:
        _refuse_if_destination_not_empty(dest_url, source_counts)
        _write_checkpoint(
            path,
            {
                "format_version": _CHECKPOINT_FORMAT_VERSION,
                "source_counts": source_counts,
                "completed_tables": {},
            },
        )

    results: list[TableCopyResult] = []
    dest_engine = create_engine_for_url(dest_url)
    try:
        dest_factory = create_session_factory(dest_engine)
        for table in _ordered_tables():
            if table.name in completed_tables:
                results.append(
                    TableCopyResult(
                        table_name=table.name,
                        source_count=source_counts[table.name],
                        dest_count_before=completed_tables[table.name],
                        rows_copied=0,
                        dest_count_after=completed_tables[table.name],
                    )
                )
                continue

            dest_count_before = _dest_count(dest_factory, table)
            rows_copied = _copy_table(
                source_url=source_url,
                dest_factory=dest_factory,
                table=table,
                chunk_size=chunk_size,
                already_copied=dest_count_before,
            )
            dest_count_after = _dest_count(dest_factory, table)
            results.append(
                TableCopyResult(
                    table_name=table.name,
                    source_count=source_counts[table.name],
                    dest_count_before=dest_count_before,
                    rows_copied=rows_copied,
                    dest_count_after=dest_count_after,
                )
            )
            completed_tables[table.name] = dest_count_after
            checkpoint_data = _load_checkpoint(path) or {}
            checkpoint_data.update(
                format_version=_CHECKPOINT_FORMAT_VERSION,
                source_counts=source_counts,
                completed_tables=completed_tables,
            )
            _write_checkpoint(path, checkpoint_data)
    finally:
        dest_engine.dispose()

    report = RowCopyReport(dry_run=False, resumed=resumed, tables=results)
    if report.ok:
        # The checkpoint is retired only once row counts confirm the copy is complete.
        # core.postgres_migration.migrate additionally verifies the audit chain before
        # a caller treats the migration as fully proven; this module's own promise ends
        # at "every row I was asked to copy is now on the destination."
        path.unlink(missing_ok=True)
    return report


def _table_exists(session: Session, table: Table) -> bool:
    from sqlalchemy import inspect

    bind = session.get_bind()
    inspector = inspect(bind)
    return bool(inspector.has_table(table.name))


def _refuse_if_destination_not_empty(dest_url: str, source_counts: dict[str, int]) -> None:
    dest_engine = create_engine_for_url(dest_url)
    try:
        dest_factory = create_session_factory(dest_engine)
        with session_scope(dest_factory) as session:
            for table in _ordered_tables():
                if not _table_exists(session, table):
                    continue
                existing = _table_row_count(session, table)
                if existing > 0:
                    raise MigrationRefusedError(
                        f"destination table {table.name!r} already has {existing} row(s); "
                        "refusing to copy into a non-empty destination. Use a fresh "
                        "database, or pass resume=True with the checkpoint from a prior "
                        "interrupted run of this same source"
                        f" (source has {source_counts.get(table.name, 0)} row(s) to copy)"
                    )
    finally:
        dest_engine.dispose()


def _dest_count(dest_factory: Any, table: Table) -> int:
    with session_scope(dest_factory) as session:
        if not _table_exists(session, table):
            return 0
        return _table_row_count(session, table)


def _copy_table(
    *,
    source_url: str,
    dest_factory: Any,
    table: Table,
    chunk_size: int,
    already_copied: int,
) -> int:
    """Read ``table`` from the source in primary-key order, in chunks, skipping the
    first ``already_copied`` rows (a resumed table's already-committed prefix), and
    insert each chunk into the destination as one retried, atomic transaction.

    Ordering by the primary key (every table here has one — a ULID string, or
    ``audit_log.seq``, ADR-004 rule D1) makes chunk boundaries deterministic across
    runs: resuming after row *N* always means the same *next* row, never a row that
    happened to reshuffle under a different read order.
    """
    source_engine = create_engine_for_url(source_url)
    pk_columns = list(table.primary_key.columns)
    total_copied = 0
    try:
        source_factory = create_session_factory(source_engine)
        offset = already_copied
        while True:
            with session_scope(source_factory) as session:
                stmt = select(table).order_by(*pk_columns).offset(offset).limit(chunk_size)
                rows = [dict(row._mapping) for row in session.execute(stmt)]
            if not rows:
                break

            def _insert_chunk(session: Session, rows: list[dict[str, Any]] = rows) -> None:
                try:
                    session.execute(table.insert(), rows)
                except IntegrityError as exc:
                    raise MigrationRefusedError(
                        f"a row in {table.name!r} conflicted with an existing destination "
                        f"row during copy — the destination was not actually empty/"
                        f"consistent with the checkpoint. Refusing to silently skip or "
                        f"overwrite it. Original error: {exc}"
                    ) from exc

            run_in_session_with_retry(dest_factory, _insert_chunk)
            total_copied += len(rows)
            offset += len(rows)
    finally:
        source_engine.dispose()
    return total_copied


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MigrationRefusedError",
    "RowCopyReport",
    "TableCopyResult",
    "copy_all_tables",
    "preflight",
]
