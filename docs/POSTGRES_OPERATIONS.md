# PostgreSQL operations

v2's production store (ADR-004, BUILD_PLAN section 8). SQLite remains the default —
nothing here is required to run Operator; it exists for the organization that has
outgrown a single-writer local file.

## Five-minute local setup

A pinned, loopback-only PostgreSQL instance for development, identical in shape to the
one CI's `postgres` job uses (`.github/workflows/ci.yml`):

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d
```

Wait for it to report healthy (`docker ps` shows `(healthy)`, usually within a few
seconds), then point the operator at it:

```bash
export N8N_OPERATOR_DATABASE_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run n8n-operator db init
uv run n8n-operator db status
```

`db status` reports the resolved (password-redacted) URL, the current and head Alembic
revision, and a live connectivity probe (reachability and latency) — never the
database's own credential. Tear the instance down with:

```bash
docker compose -f docker/postgres-test/docker-compose.yml down -v
```

`-v` also removes the named volume — the container is meant to be disposable between
test runs, never a place to keep data you care about.

## Migrating an existing v1 SQLite database

`n8n-operator db migrate-to-postgres` copies every row from a running v1 SQLite
database onto a PostgreSQL destination, once, with proof it worked — see ADR-004 and
`storage/postgres_migration.py`'s module docstring for the full design. The short
version, from the machine that already holds the SQLite file:

```bash
# 1. See what would happen — no write, no schema change on the destination.
uv run n8n-operator db migrate-to-postgres \
  --dest "postgresql+psycopg://operator@db.internal:5432/n8n_operator" \
  --dry-run

# 2. Run it for real. Brings the destination schema to head automatically
#    (equivalent to `db migrate` against the destination) and then copies every table.
uv run n8n-operator db migrate-to-postgres \
  --dest "postgresql+psycopg://operator@db.internal:5432/n8n_operator"
```

A password never has to appear on the command line: omit it from `--dest` and set
`N8N_OPERATOR_DATABASE_PASSWORD` instead (a literal value, `env:NAME`, or
`keyring:SERVICE/ACCOUNT` — the same indirection `N8N_OPERATOR_N8N_API_KEY` accepts,
ADR-006). Every line this command prints redacts the password unconditionally.

**Interrupted midway?** The command checkpoints progress to disk (a JSON file, default
path derived only from the destination's host/database name — never from a URL that
could carry a credential) after every table it finishes. Re-run with `--resume` to
continue exactly where it left off:

```bash
uv run n8n-operator db migrate-to-postgres \
  --dest "postgresql+psycopg://operator@db.internal:5432/n8n_operator" \
  --resume
```

A resume that no longer matches the source (row counts differ from what the checkpoint
recorded — the SQLite database kept changing after the interrupted attempt) is refused
outright rather than silently reconciled; delete the checkpoint file named in the error
and start over once the source is quiescent.

**A destination that already has rows in it is refused**, whether or not a checkpoint
exists for it, unless that checkpoint is the one this exact tool wrote. Point the
command at a fresh, empty database — running Alembic migrations on an already-populated
destination that predates this tool is not something `migrate-to-postgres` does.

Once the command reports "Migration verified," `database_url` (and, if set,
`database_password`) can be switched over to PostgreSQL for the actual deployment. The
source SQLite file is never modified by any of this — keep it until you are confident
in the cutover, then archive or discard it per your own retention policy (BUILD_PLAN
section 8.2 has no opinion on SQLite files that are no longer the operator's live
database).

## Backup

Use PostgreSQL's own tooling — Operator does not ship a bespoke backup mechanism, the
same way it never invented one for SQLite (a file-level copy already sufficed there).

```bash
pg_dump --format=custom --file=n8n_operator_$(date +%Y%m%dT%H%M%S).dump \
  "postgresql://operator@db.internal:5432/n8n_operator"
```

The custom format (`--format=custom`) is `pg_restore`-only but supports selective
restore and is meaningfully smaller than plain SQL for a database whose largest column
(`execution_results.redacted_payload`) can approach `output.max_bytes` per row. Take a
backup before every `db migrate` against a production destination, exactly as you would
before any schema change — Alembic migrations in this codebase are additive-only so
far (BUILD_PLAN section 8.3's stage-01 additions are all new tables or nullable
columns), but that is not a guarantee future migrations will keep.

## Restore

```bash
createdb -h db.internal -U operator n8n_operator_restored
pg_restore --dbname="postgresql://operator@db.internal:5432/n8n_operator_restored" \
  n8n_operator_20260828T120000.dump
uv run n8n-operator db status  # confirm it lands at the revision the backup expects
```

Restoring into a fresh, differently-named database first (rather than directly
overwriting the live one) lets you run `audit verify` and spot-check `db status`
against the restored copy before cutting the running server over to it — the same
"verify before you trust it" discipline `migrate-to-postgres` already applies to a
fresh copy.

## Rollback

**A bad Alembic migration**, caught before other writes have landed on top of it:

```bash
uv run alembic -x db_url="postgresql+psycopg://operator@db.internal:5432/n8n_operator" downgrade -1
```

(`storage/migrations/env.py` documents the same `-x db_url=` resolution the `db` CLI
commands use internally.) Every migration in this codebase ships a real `downgrade()` —
verified in CI by `tests/integration/test_migrations.py`'s round-trip check — so this is
always available, not aspirational.

**A bad cutover to PostgreSQL entirely**: point `N8N_OPERATOR_DATABASE_URL` back at the
original SQLite file (never modified by the migration, per the section above) and
restart. Nothing about the SQLite-era database becomes invalid by having been copied
elsewhere; the copy is additive, not destructive. Rehearsed for real against a v2-shaped
dataset (organizations, environments, memberships, an anchored audit chain) in
`tests/integration/postgres/test_v2_migration_rehearsal.py::test_rollback_restores_the_pre_migration_sqlite_file_untouched`,
which byte-compares the source SQLite file before and after `migrate()` runs — see
`docs/evidence/stage11-migration-rehearsal.md` for the recorded result.

**A migration that "succeeded" but failed verification** (`migrate-to-postgres` exits
`2`): treat the destination as unproven, not as a fallback with a known-good subset.
A row-count mismatch or a broken audit chain on the destination means something is
wrong with *that copy specifically* — investigate before either retrying against a
fresh destination or discarding it; do not attempt to hand-patch the destination into
matching, which would defeat the entire point of independently re-verifying it.

## Capacity assumptions and connection pooling

Every engine `storage/session.py` creates for PostgreSQL uses a bounded pool:
`database_pool_size` (default 5) steady connections per process, up to
`database_max_overflow` (default 10) more under burst load, a `pool_timeout` (default
30s) a caller waits before giving up on a connection, `pool_recycle` (default 1800s)
so no connection outlives most managed providers' own idle-kill windows, and
`pool_pre_ping` always on (a connection a managed provider silently dropped is detected
and replaced before a caller ever sees it fail). A `statement_timeout` (default 30s) is
set on every new PostgreSQL connection — every query this codebase issues is a simple,
indexed lookup or a bounded chunked copy; anything needing longer than that is a bug,
not a workload that should be accommodated by raising the ceiling by default.

**Sizing for your deployment**: each Operator process (an MCP stdio server, an HTTP
server, a CLI invocation) opens its own pool. `pool_size + max_overflow` is the
*ceiling* that one process can hold open at once, not what it holds constantly — most
of the time far fewer are in use. Multiply by however many concurrent Operator
processes your deployment runs, and keep the total comfortably under PostgreSQL's own
`max_connections` (100 by default on a stock install) — leave headroom for `psql`,
monitoring tools, and PostgreSQL's own reserved superuser connections. A managed
Postgres offering (RDS, Cloud SQL, etc.) often sets `max_connections` lower than the
stock default for a small instance size; check the provider's own documentation before
assuming 100.

Every pooling/timeout value is a `Settings` field
(`N8N_OPERATOR_DATABASE_POOL_SIZE`, `N8N_OPERATOR_DATABASE_MAX_OVERFLOW`,
`N8N_OPERATOR_DATABASE_POOL_TIMEOUT_SECONDS`, `N8N_OPERATOR_DATABASE_POOL_RECYCLE_SECONDS`,
`N8N_OPERATOR_DATABASE_STATEMENT_TIMEOUT_SECONDS`, `N8N_OPERATOR_DATABASE_CONNECT_TIMEOUT_SECONDS`)
— tune per deployment rather than editing code.

## Connection exhaustion

If every pooled connection is checked out and the queue exceeds `pool_timeout`, a
caller sees a plain SQLAlchemy `TimeoutError` — not a hang, not a silently-dropped
request. `db status`'s connectivity probe opens one connection outside the pool
(`create_engine_for_url` is called fresh for that check) and will still succeed even
while the *application's* pool is fully checked out, which is useful for telling apart
"the database itself is unreachable" from "this specific process's pool is saturated."

If you are seeing exhaustion:

1. Check how many Operator processes are actually running against this database —
   pool exhaustion from a single process usually means genuinely concurrent load
   (several team members preparing/approving operations at once, which is exactly what
   v2 is for) rather than a leak.
2. Confirm connections are being returned: every session this codebase opens goes
   through `storage.session.session_scope` or `run_in_session_with_retry`, both of
   which close their session in a `finally` block unconditionally — a held-open
   connection past a request's lifetime would be a bug in a caller bypassing that
   discipline, not expected behavior anywhere in the shipped code.
3. Raise `database_pool_size`/`database_max_overflow` only after confirming headroom
   against PostgreSQL's own `max_connections` (see Capacity assumptions above) — raising
   the pool ceiling without raising (or without available) server-side headroom just
   moves the failure from "this process times out" to "PostgreSQL itself refuses new
   connections," which is strictly worse to diagnose.
