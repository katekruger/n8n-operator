# Stage 11 — Load and Concurrency Test Results

Produced by `scripts/load_test.py` (a dependency-free, in-process Python threading
harness — no locust/k6, matching this repo's zero-heavyweight-tooling convention).
Run manually; not part of CI.

## Published assumptions

These numbers describe one specific setup, not an internet-scale claim:

- **Hardware**: this machine's own hardware (whatever ran the command below — a
  single developer laptop, not a provisioned benchmark host).
- **Database**: a local, loopback-only Postgres 16 container
  (`docker/postgres-test/docker-compose.yml`), bound to `127.0.0.1:55432`, not shared
  with anything else on the host.
- **Transport**: in-process Python threading stands in for MCP transport (stdio or a
  network hop). This measures the governed-write pipeline's own overhead —
  argument validation, rate-limit/idempotency checks, environment/authorization
  resolution, operation/audit persistence inside `service.prepare_operation` — not
  MCP transport latency, and not a real n8n instance (preflight is faked; no
  `execute_operation`/`approve_operation` call is made, so this does not measure
  n8n's own webhook latency).
- **Scope**: only `prepare_operation` is exercised (workflow/environment resolution,
  authorization, idempotency, argument-size and rate-limit checks, and operation +
  audit-log persistence in one transaction). Approval and execution are out of scope
  for this harness.

## Profiles

| Profile   | Concurrent operators | Total operations | Environments | Quorum fraction |
|-----------|----------------------|-------------------|---------------|------------------|
| startup   | 5                     | 50                | 1             | 0.0              |
| seriesc   | 50                    | 5,000             | 3             | 0.2              |

(`quorum_fraction` is published as part of the profile shape for future extension —
this harness's `_worker`/`run_profile` currently exercises `prepare_operation` only,
not the multi-approver quorum path, so it does not yet affect these numbers.)

## Commands run

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d

uv run python scripts/load_test.py \
  --database-url "postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/n8n_operator_load_startup" \
  --profile startup --create-database

uv run python scripts/load_test.py \
  --database-url "postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/n8n_operator_load_seriesc" \
  --profile seriesc --create-database
```

`--create-database` (added beyond the brief's sketch) connects to the target
server's own `postgres` maintenance database and issues `CREATE DATABASE` if the
target doesn't already exist — the same approach
`tests/integration/conftest.py`'s `postgres_test_db_url` fixture uses programmatically
for the Postgres integration suite, just invoked by hand here for this manual
rehearsal instead of via a pytest fixture.

## Results

### `startup` profile

```
Running profile 'startup': 5 concurrent operators, 50 total operations, 1 environment(s).

Wall clock: 0.23s
Total operations attempted: 50
Errors: 0 (0.00%)
Throughput: 221.90 ops/sec
Latency p50: 18.4ms
Latency p95: 57.4ms
Latency p99: 58.5ms
Latency mean: 22.0ms
```

### `seriesc` profile

```
Running profile 'seriesc': 50 concurrent operators, 5000 total operations, 3 environment(s).

Wall clock: 46.62s
Total operations attempted: 5000
Errors: 0 (0.00%)
Throughput: 107.25 ops/sec
Latency p50: 484.9ms
Latency p95: 851.3ms
Latency p99: 1012.6ms
Latency mean: 465.2ms
```

Both runs completed with **zero errors** — no `RateLimitedError`,
`ArgumentsTooLargeError`, deadlock, or other exception surfaced across 5,050 attempted
operations. Latency rises substantially under the `seriesc` profile's higher
concurrency (50 threads vs. 5, all serializing through the same Postgres connection
pool and `operations`/`audit_log` tables), which is expected — this is exactly the
lock/pool contention this harness exists to characterize — but throughput stays
comfortably above every registry-configured `rate_limit_per_minute` (see below), and
no operation failed or hung.

## Registry rate-limit defaults vs. measured throughput

`examples/registry/starter-kits/gtm-starter-kits.yaml` and
`examples/registry/workflows.example.yaml` set `rate_limit_per_minute` values ranging
from 2 (`crm.bulk_update_stage`) to 20 (the highest configured value in either file).
Converted to ops/sec, that's roughly 0.03–0.33 ops/sec **per workflow**.

Measured system-wide `prepare_operation` throughput here was 221.9 ops/sec
(`startup`, low concurrency) and 107.25 ops/sec (`seriesc`, high concurrency, more
pool/lock contention) — both several orders of magnitude above any single workflow's
configured per-minute limit. This confirms what the brief anticipated: these limits
are **approval-workflow-driven** (how many risky external writes a human approval
process can reasonably absorb per minute), not **infrastructure-driven** (the system
never comes close to choking on them). The `seriesc` run's higher latency under load
(p99 ~1s at 50-way concurrency) is still far below the ~3s/op budget a 20/minute
limit would imply even in the worst case, so nothing here suggests a configured limit
is actually unreachable in practice.

**Conclusion: no registry edit needed.** The existing `rate_limit_per_minute` values
in both files remain appropriate as-is; this load test did not reveal a limit that
chokes before its configured value under realistic concurrency.

## Discrepancies from the task brief

None. Every signature the brief's Step 1–3 code depends on
(`service.prepare_operation`, `service.reload_registry`,
`OrganizationRepository.create`, `EnvironmentRepository.create`,
`OrganizationMembershipRepository.create`, `PrincipalRepository.create`,
`create_engine_for_url`/`create_session_factory`/`session_scope`,
`n8n_operator.cli.commands.db._alembic_config`) matched the brief exactly against the
actual source in `src/n8n_operator/core/service.py` and
`src/n8n_operator/storage/repository.py` — the script ran with the brief's Step 1–3
code unmodified beyond:

- Typing `FakePreflight.check`'s `workflow` parameter as
  `n8n_operator.core.models.WorkflowContract` instead of `Any`, to match
  `PreflightPort`'s protocol exactly under `mypy --strict` (both pass; this is a
  strictness improvement, not a functional change).
- Adding a `--create-database` CLI flag (the brief left the choice open) that issues
  `CREATE DATABASE` against the target server's `postgres` maintenance database if
  the target database doesn't already exist yet, rather than requiring the operator
  to run `psql` by hand first.
- Two `ruff` fixes: an unused `noqa: BLE001` (that rule isn't enabled in this
  project's ruff config, so the directive was flagged as unused) and a `noqa: S108`
  on the `/tmp` registry-file path (a manual dev script, not shipped code).
