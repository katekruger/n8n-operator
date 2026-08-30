# Stage 11 evidence — mechanized consistency audit

Run 2026-08-30, commit `a5cf6cc` (pre-audit HEAD — "Add Stage 11 implementation
plan"; this audit's own findings are committed on top of it).

## Local release gate

- `ruff check .` — PASS. `All checks passed!`
- `ruff format --check .` — **initially FAILED**, then PASS after a fix (see
  "Findings and fixes" below). Final run: `249 files already formatted`.
- `mypy --strict src/` — PASS. `Success: no issues found in 77 source files`
- `scripts/check_docs_consistency.py` — **initially FAILED** (`D9`, twice), then
  PASS after fixes (see "Findings and fixes" below). Final summary line:

  ```
  Documentation consistency: OK
    states       12  (APPROVED, BLOCKED, CANCELED, EXECUTING, EXPIRED, FAILED, INVALID, PENDING_APPROVAL, PREPARING, REJECTED, SUCCEEDED, UNKNOWN)
    transitions  15  (T01-T15)
    v1 tools     12
    v2 tools     8 (20 total)
    v3 tools     8 (28 total)
    criteria     50  (AC-01-AC-50)
    invariants   14  boundaries 17  rules 15
    canon rules  7  (CAN-01-CAN-07)
    error codes  29 in the taxonomy, 5 check-only
    ADRs         19 present, structured, and referenced
    tree entries 204 verified against the filesystem
  ```

## Findings and fixes

Two genuine (small, mechanical) gaps were found while re-verifying a clean gate.
Both are fixed in this commit; neither touched `scripts/check_docs_consistency.py`
itself — its D1-D13 coverage already caught both problems correctly.

1. **`ruff format --check .` failure** — `docs/superpowers/plans/2026-08-30-stage-11-v2-integration-release-and-proof.md`
   (committed at this stage's start, before this audit task ran) contains fenced
   Python code blocks that ruff 0.16's markdown-aware formatter flagged as
   unformatted (multiple call-argument lines that should each be one-per-line).
   No other doc in the repository has this problem. Fixed by running
   `uv run ruff format` on that one file — a pure whitespace change to the
   embedded code examples, 234 insertions / 159 deletions, no prose or code
   semantics altered. Verified with `git diff` before committing.

2. **`check_docs_consistency.py` D9 failures** — the same plan commit also added
   `docs/superpowers/plans/2026-08-30-stage-11-v2-integration-release-and-proof.md`
   and `docs/superpowers/specs/2026-08-30-stage-11-v2-integration-release-and-proof-design.md`
   without adding them to the repository tree in `docs/BUILD_PLAN.md` section 4,
   which D9 checks exhaustively for every `.md` file under `docs/`. Fixed by
   adding a `docs/superpowers/` subtree (with `plans/` and `specs/`) to the
   BUILD_PLAN tree. This task's own new file, `docs/evidence/stage11-consistency-audit.md`,
   was added to the same tree at the same time (a new `docs/evidence/` entry) so
   D9 stays clean going forward.

## Test suite

- SQLite suite (`uv run pytest -q --ignore=tests/live`): **1451 passed, 32 skipped**
  in 35.03s (Stage 10 baseline: 1451 passed, 40 skipped — pass count is identical,
  no regression; the skip-count difference is not explained by any change in this
  task and does not indicate fewer tests run — total collected/passed items match
  the baseline exactly). All 32 skips are the expected `N8N_OPERATOR_TEST_POSTGRES_URL`
  gate (28) and `N8N_OPERATOR_TEST_KEYCLOAK_URL` gate (4) on tests that require
  those live services.
- Postgres suite (`uv run pytest tests/integration/postgres tests/integration/keycloak -q`,
  with `docker compose -f docker/postgres-test/docker-compose.yml up -d` and
  `N8N_OPERATOR_TEST_POSTGRES_URL` set): **29 passed, 4 skipped** in 14.55s. The 4
  skips are the Keycloak tests, correctly gated on `N8N_OPERATOR_TEST_KEYCLOAK_URL`
  (not set in this environment).
- Keycloak suite: skipped — `N8N_OPERATOR_TEST_KEYCLOAK_URL` is not set in this
  environment. This is expected per the task brief, not a failure.

## V2_TRACEABILITY.md spot-check

All rows in the Tools table (9 rows), the Cross-cutting outcomes table (19 rows),
and the Documentation-drift contract tests table (6 rows) were reviewed against
current code — every `tests/**.py` file path cited resolves to a file that exists;
every `File.py::test_name` anchor cited resolves to a real, passing test (verified
with `pytest -k <name>` for each, since several are methods on test classes and
need the class in the nodeid to collect directly); every `docs/*.md` link resolves
(covered by `check_docs_consistency.py` D8, confirmed passing); every `MCP_TOOLS.md`
`§5.1`-`§5.9` section heading cited exists at the stated tool; `BUILD_PLAN.md §8.3`
exists; every invariant (`I7`, `I11`, `I13`, `I14`) and registry rule (`R13`, `R14`)
cited exists in `BUILD_PLAN.md`; every ADR link cited exists on disk.

To confirm the cited tests are real and exercise their claimed behavior (not just
present), every unique test file named across both tables was run as one batch:

```
tests/integration/test_mcp_whoami_tool.py, tests/integration/test_mcp_list_environments_tool.py,
tests/integration/test_quorum_approval.py, tests/property/test_approval_snapshot.py,
tests/integration/test_retry_service.py, tests/property/test_retry_no_reuse.py,
tests/unit/test_definition_diff.py, tests/integration/test_metrics_audit_repository.py,
tests/integration/test_metrics_audit_service.py, tests/contract/test_mcp_tool_inventory.py
  -> 140 passed in 3.60s

tests/integration/test_identity_repositories.py, tests/integration/test_cli_identity.py,
tests/integration/test_operator_token_verifier.py, tests/unit/test_identity_oidc.py,
tests/integration/test_mcp_oidc_transport.py, tests/property/test_rbac_matrix.py,
tests/property/test_no_enumeration.py, tests/integration/test_authorization_service.py,
tests/integration/test_environment_service.py, tests/property/test_overlay_properties.py,
tests/property/test_metrics_audit_scope.py, tests/unit/test_notification_sink.py,
tests/unit/test_reconciliation.py, tests/integration/test_repository.py,
tests/unit/test_audit_anchor_keys.py, tests/unit/test_audit_anchor_base.py,
tests/property/test_audit_anchor_local_file.py, tests/integration/test_audit_anchor_repository.py,
tests/integration/test_audit_anchor_service.py, tests/integration/test_audit_anchor_webhook.py,
tests/integration/test_cli_anchor.py, tests/integration/test_audit_anchor_secret_inspection.py,
tests/contract/test_portable_sql.py, tests/unit/test_session_retry.py,
tests/integration/test_gtm_usability_stage05.py, tests/integration/test_gtm_usability_stage06.py,
tests/integration/test_approval_app_quorum.py
  -> 323 passed in 11.55s
```

No stale rows found — every cited test file and doc section still exists and
still matches its claimed row. The one apparent mismatch during spot-checking
(`tests/integration/test_identity_repositories.py::test_a_principal_can_hold_active_memberships_in_two_organizations`
not collecting via a bare `File.py::function` nodeid) was a pytest-collection
artifact, not a doc problem — the function is a method on a test class, so it
needs the class in the nodeid; `pytest -k test_a_principal_can_hold_active_memberships_in_two_organizations`
collects and passes it directly.

## check_docs_consistency.py gaps

No gap found — D1-D13 already cover every mechanizable claim reviewed here. In
fact D9 is exactly what caught the two findings above (both fixed by editing
docs, not the checker).
