# ADR-004: SQLite in v1, PostgreSQL in v2, one schema throughout

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) section 8, [ARCHITECTURE.md](../ARCHITECTURE.md) section 6

## Context

v1 is a single operator running a local tool. Requiring PostgreSQL means requiring
Docker or a managed database before anyone can try the product — a heavy toll for
something that will hold a few thousand rows.

v2 is teams, organizations, and concurrent approvals. SQLite's single-writer model and
weak concurrency story stop being acceptable there, and a hosted deployment needs a real
database.

The failure mode to avoid is the usual one: build on SQLite, quietly depend on its
behavior, then discover at v2 that the migration is a rewrite. The dependencies are
rarely obvious — they hide in `AUTOINCREMENT` primary keys, in naive datetimes that
happened to compare correctly, in `INSERT OR REPLACE`, in relying on a single writer for
correctness that Postgres will not provide.

## Decision

**Ship SQLite in v1 and PostgreSQL in v2 with one schema and one ORM, by writing only
portable constructs from the first migration.**

Binding rules for all v1 code:

| # | Rule | Why |
|---|---|---|
| D1 | Primary keys are ULID strings, never `AUTOINCREMENT` or `SERIAL`. | Identical in both engines; sortable; safe to generate client-side. |
| D2 | Timestamps are timezone-aware UTC via SQLAlchemy `DateTime(timezone=True)`. Never naive, never a formatted string. | SQLite has no native datetime; naive values compare wrongly after a Postgres move. |
| D3 | Structured columns use the SQLAlchemy `JSON` type, never engine-specific `JSONB`. | Portable. v2 may add a Postgres-only index without changing the column type. |
| D4 | No engine-specific SQL: no `INSERT OR REPLACE`, no `ON CONFLICT` variants, no `RETURNING`, no `PRAGMA` outside connection setup. | These do not translate. |
| D5 | All access goes through SQLAlchemy ORM or Core. No raw SQL strings outside migrations. | One dialect layer to change. |
| D6 | Every schema change is an Alembic migration. `create_all` is used only in tests. | The v2 migration is a continuation of one history, not a new beginning. |
| D7 | Concurrency correctness never relies on SQLite's single writer. Every mutation of `operations` carries a `state_version` optimistic-concurrency guard; the handle burn is an explicit compare-and-set. | Postgres allows concurrent writers; logic that was safe by accident would break. |
| D8 | Uniqueness is enforced by database constraints, not application checks. | Both engines enforce them; application checks race in both. |
| D9 | SQLite runs in WAL mode with a busy timeout, configured at connection setup only. | Reasonable local concurrency without leaking into the schema. |
| D10 | Enum-like columns are `text` with a `CHECK` constraint, not native enum types. | Native enums differ sharply between engines and are painful to alter. |

Verified continuously by AC-24: Alembic autogenerate against the ORM metadata must
produce an empty diff, so schema and models cannot silently diverge.

## Consequences

### Positive

- v1 installs with `uv sync` and runs. No container, no server, no connection string.
- The v2 migration is configuration plus a data copy, not a redesign. `DATABASE_URL`
  changes; the ORM, the repository layer, and the migration history do not.
- Portable-only constructs keep the data layer boring, which is the correct temperament
  for code that holds an audit log.
- Tests run against real SQLite with real migrations, so the migration path is exercised
  on every commit rather than once at the v2 boundary.

### Negative

- We forgo genuinely useful Postgres features in v1: `JSONB` indexing, partial indexes,
  `RETURNING`, advisory locks, real enums.
- Optimistic concurrency is more code than relying on SQLite's global write lock.
- `CHECK`-constrained text columns are less self-documenting than native enums.
- SQLite will not scale past a handful of concurrent approvers, which is precisely why
  v2 exists.

### Neutral

- SQLAlchemy 2.0 typed declarative style (`Mapped[...]`, `mapped_column`) throughout, so
  `mypy --strict` covers the data layer.
- The v2 data migration is a straightforward ORM-level copy, since every type is
  portable by construction. The audit chain re-verifies after the copy — a broken chain
  post-migration is a migration bug, and the check is how we would find it.

## Alternatives considered

**PostgreSQL from v1.** Correct destination, wrong on-ramp. Rejected: it triples the
setup cost of a single-user tool and would meaningfully reduce the number of people who
try it.

**SQLite forever, with a scaling story elsewhere.** Rejected: team approvals require
concurrent writers and a network-reachable database.

**Database-agnostic abstraction layer over both.** Rejected as over-engineering.
SQLAlchemy already is that layer; a second one adds indirection without adding safety.

**Document store or event log instead of a relational schema.** Rejected: the data is
strongly relational, uniqueness constraints are load-bearing security controls (D8), and
the audit chain wants ordered, immutable rows — which relational databases do well.
