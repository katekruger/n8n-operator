"""Orchestrates the SQLite -> PostgreSQL data migration across two capability
packages: the row copy itself (``storage.postgres_migration``) and the audit hash
chain's independent re-verification on the destination (``audit.chain``, via
``storage.repository.AuditLogRepository`` — the same two pieces
``verify_audit_chain`` already composes for ``n8n-operator audit verify``).

This composition belongs in ``core/`` rather than in ``storage/`` for exactly the
reason ``core/service.py``'s own module docstring gives for orchestration generally:
``storage`` and ``audit`` are sibling capability packages and must not import each
other or know about each other's existence (ARCHITECTURE.md section 2.1, ADR-001) — a
migration tool that needs both is, definitionally, core's job.

Phase 10 (v2) stage 01.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from n8n_operator.audit.chain import ChainVerificationResult
from n8n_operator.core.service import verify_audit_chain
from n8n_operator.storage.postgres_migration import (
    DEFAULT_CHUNK_SIZE,
    MigrationRefusedError,
    RowCopyReport,
    TableCopyResult,
    copy_all_tables,
    preflight,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MigrationRefusedError",
    "MigrationReport",
    "TableCopyResult",
    "migrate",
    "preflight",
]


@dataclass(frozen=True)
class MigrationReport:
    """The full proof a v2 migration needs: every table's row counts, plus the
    destination's audit hash chain independently re-verified end to end."""

    row_copy: RowCopyReport
    audit_chain: ChainVerificationResult

    @property
    def dry_run(self) -> bool:
        return self.row_copy.dry_run

    @property
    def resumed(self) -> bool:
        return self.row_copy.resumed

    @property
    def tables(self) -> list[TableCopyResult]:
        return self.row_copy.tables

    @property
    def ok(self) -> bool:
        return self.row_copy.ok and self.audit_chain.ok


def migrate(
    *,
    source_url: str,
    dest_url: str,
    dry_run: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> MigrationReport:
    """Copy every row from ``source_url`` (SQLite) to ``dest_url`` (PostgreSQL), then
    independently re-verify the destination's audit hash chain — the entry point real
    callers (the CLI) should use. See ``storage.postgres_migration.copy_all_tables``
    for the row-copy mechanics this wraps.

    A dry run performs no destination write and therefore has nothing to re-verify: the
    audit-chain result is trivially ``ok`` (an empty chain verifies) and carries no
    information — callers should gate on ``dry_run`` before reading it, exactly as they
    would for ``row_copy``.
    """
    row_copy = copy_all_tables(
        source_url=source_url,
        dest_url=dest_url,
        dry_run=dry_run,
        chunk_size=chunk_size,
        checkpoint_path=checkpoint_path,
        resume=resume,
    )

    if dry_run:
        return MigrationReport(
            row_copy=row_copy,
            audit_chain=ChainVerificationResult(ok=True, first_break_seq=None, reason=None),
        )

    dest_engine = create_engine_for_url(dest_url)
    try:
        dest_factory = create_session_factory(dest_engine)
        with session_scope(dest_factory) as session:
            audit_chain = verify_audit_chain(session)
    finally:
        dest_engine.dispose()

    return MigrationReport(row_copy=row_copy, audit_chain=audit_chain)
