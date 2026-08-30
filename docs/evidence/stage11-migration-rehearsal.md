# Stage 11 evidence — PostgreSQL migration rehearsal (real run plus rollback)

Run 2026-08-30 against a real, pinned, loopback PostgreSQL instance
(`docker compose -f docker/postgres-test/docker-compose.yml up -d`, then
`N8N_OPERATOR_TEST_POSTGRES_URL=postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres`),
via `uv run pytest tests/integration/postgres/test_v2_migration_rehearsal.py -v`. Both
tests **PASSED**:

```
tests/integration/postgres/test_v2_migration_rehearsal.py::test_v2_shaped_dataset_migrates_with_verified_counts_and_intact_anchor_chain PASSED
tests/integration/postgres/test_v2_migration_rehearsal.py::test_rollback_restores_the_pre_migration_sqlite_file_untouched PASSED
2 passed in 2.00s
```

This extends the existing v1-only migration coverage in
`tests/integration/postgres/test_migration.py` (unmodified) with a v2-shaped dataset:
one organization, one environment, one operator+admin membership, plus the existing
v1 rows (a principal, a registry snapshot) and — new here — a real, signed, anchored
audit chain via `LocalFileAnchor`/`core.service.publish_anchor`.

## Row-count table (before -> after)

Every table in `Base.metadata` was counted in the source SQLite database immediately
before `migrate()` ran, and again in the destination PostgreSQL database immediately
after. Only tables with at least one row in this fixture are listed; every other table
in the schema was confirmed at `0 -> 0`, i.e. present in the destination schema and
empty on both sides, consistent with `test_migration.py`'s existing empty-database
coverage.

| Table                       | Before (SQLite) | After (PostgreSQL) | Match |
|------------------------------|:---------------:|:-------------------:|:-----:|
| `principals`                 | 2                | 2                    | yes   |
| `registry_snapshots`         | 1                | 1                    | yes   |
| `organizations`               | 1                | 1                    | yes   |
| `environments`                | 1                | 1                    | yes   |
| `organization_memberships`    | 1                | 1                    | yes   |
| `audit_log`                   | 1                | 1                    | yes   |
| `audit_anchors`               | 1                | 1                    | yes   |

(`principals` holds 2 rows because the fixture creates both the `kind="local"`
principal the v1 seed pattern always includes, and a `kind="user"` principal —
"Rehearsal Operator" — who holds the `operator` and `admin` roles used to publish the
anchor.)

The test does not stop at raw counts: after migrating, it re-fetches the seeded
organization and environment from the **PostgreSQL** database by their original IDs
(`OrganizationRepository.get`/`EnvironmentRepository.get`) and asserts the environment
still points at the correct organization — proving primary keys and the
`environments.organization_id` foreign key survived the copy unchanged, not just that
row totals happened to match.

## Audit-chain integrity result

Before migration, the test seeds one real audit-log entry (`create_organization`) and
publishes it through `LocalFileAnchor` (via `cli/commands/anchor.py`'s
`_ServiceSinkAdapter`, admin-gated through `core.service.publish_anchor` with
`enable_v2=True`) into a signed, append-only anchor file outside either database. That
anchor file is signed against the chain tip as it existed **in SQLite**.

After migration, the test re-verifies that same anchor file — `LocalFileAnchor.verify_file()`
— using only the public key embedded in each anchor line; nothing about verification
depends on which database backend now holds the audit log. Result:

```
file_report.ok == True
file_report.lines_checked == 1
file_report.issues == []
```

This is the entire point of an external anchor (ADR-012 section 2): the signed chain
commitment was made independent of any particular database, so migrating the audit log
from SQLite to PostgreSQL cannot silently break — or silently hide a break in — the
chain's integrity. The anchor verifies exactly as it did before migration, over data
that now lives in a different database engine entirely.

## Rollback rehearsal result

The second test seeds the same v2-shaped dataset, takes a byte-for-byte copy of the
source SQLite file *before* calling `migrate()`, runs the real migration against
PostgreSQL, and then byte-compares the post-migration source file against that
pre-migration copy. Result: **byte-identical**.

This is not merely asserted in prose — it is mechanically verified on every run: the
migration's `copy_all_tables` reads from the source engine and writes to the
destination engine, and never opens the source file for writing. A byte-identical
before/after comparison is only possible if that held. Because the source file is
provably untouched, "rollback" from a bad PostgreSQL cutover has no restore step to
get wrong: it is exactly "stop pointing `N8N_OPERATOR_DATABASE_URL` at the new
PostgreSQL database and point it back at this same SQLite file," as
`docs/POSTGRES_OPERATIONS.md`'s "Rollback" section already documented before this
task — this rehearsal is the evidence that documentation's claim is not aspirational.

## Gate

`ruff check`, `ruff format --check`, and `mypy --strict` all pass clean on
`tests/integration/postgres/test_v2_migration_rehearsal.py`.
