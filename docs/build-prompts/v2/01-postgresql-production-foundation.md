# Stage 01 prompt — PostgreSQL production foundation

Copy this entire file into a fresh Claude Code session after Stage 00 is merged.

## Mission

Make PostgreSQL the supported v2 production store while preserving SQLite as a low-friction
development option. Prove migration integrity instead of merely making SQLAlchemy connect.

## Required work

- Start from the latest `main`; verify Stage 00’s contracts and entry criteria are present.
- Implement the storage configuration and engine lifecycle defined by ADR-004. Keep the
  repository layer portable unless a narrowly-scoped Postgres implementation is justified.
- Add production-safe connection pooling, transaction isolation, health checks, statement
  timeouts, UTC handling, and clean shutdown. Secrets must come only from supported secret
  sources and must never appear in logs or health results.
- Add all v2-ready organization/environment/identity columns or tables that Stage 00 has
  determined are foundational, but do not implement identity or authorization behavior yet.
- Build an idempotent SQLite-to-PostgreSQL migration command with dry-run, preflight,
  counts, checkpointing, fail-closed conflict handling, and post-copy verification.
- Re-verify the complete audit hash chain after migration. A mismatch must fail the
  migration and produce a safe diagnostic without mutating the source database.
- Add a pinned, loopback-only Postgres integration harness and CI job. Test migrations from
  an empty database and a realistic v1 fixture containing operations in every state,
  approvals, results, registry snapshots, and audit entries.
- Document backup, restore, rollback, capacity assumptions, connection exhaustion, and a
  five-minute local development setup.

## Edge cases and proof

Test concurrent operation creation, handle burning, quorum-ready writes, idempotency races,
database disconnects, serialization/deadlock handling without replaying external side
effects, timestamp precision, Unicode JSON, large capped payloads, duplicate source rows,
partial copy interruption, resumption, and destination-not-empty refusal.

The dispatch boundary must never be automatically retried because a database transaction
failed. Preserve the v1 distinction between a database retry and repeating an n8n side
effect.

## Completion gate

Run the full SQLite suite and the new Postgres suite. Require Alembic/ORM drift checks on
both stores, audit-chain equality before and after migration, package smoke tests, updated
architecture/runbook documentation, and at least 90% coverage for new core/storage logic.
Return measured migration evidence and Stage 02 entry criteria.
