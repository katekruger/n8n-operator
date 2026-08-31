# Stage 11 evidence — internal security review (Task 5, expanded)

**This is a self-conducted, internal security review performed by the engineering
team working on this codebase. It is not a substitute for professional third-party
penetration testing — no external pentest budget was available for this project.**
Findings below should be read with that limitation in mind: this review is as
thorough as static/dynamic source review plus targeted regression testing can make
it, but it was not adversarially probed by an independent party, and it did not
include infrastructure, dependency-supply-chain, or social-engineering testing.

Branch: `feat/v2-stage-11-integration-release-and-proof`.

## Summary

A prior session on this branch discovered a real, confirmed cross-tenant audit-log
leak in `AuditLogRepository.list_page` (`src/n8n_operator/storage/repository.py`).
This document records: the finding and its severity, the fix, the full new test
coverage added for it, an independent verification that `core.service.get_metrics`
was **not** vulnerable to the same class of bug, a systematic audit of every other
named v2 read surface, and the new parameterized regression guard
(`tests/integration/test_tenant_isolation_matrix.py`) meant to catch a future
instance of this bug class automatically.

Threat model entry: `docs/THREAT_MODEL.md` T-66.

## Part 1 — the confirmed finding: `AuditLogRepository.list_page`

**Severity: Critical / High** — a real, confirmed cross-tenant information
disclosure of audit-log content (not merely metadata).

### What was wrong

`list_page`'s `subject_type="operation"` branch authorizes an audit entry about an
operation by checking, via a correlated `EXISTS` subquery against `operations`,
whether that operation's own `workflow_id` matches one of the caller's
`workflow_id_like_patterns`. It never additionally checked that the operation's own
`environment_id` matched the caller's own resolved `environment_id` — unlike the
sibling `environment_clause` a few lines below it, which always has.

This deployment uses **one global workflow registry**: a `workflow_id` such as
`crm.sync_contact` is the same registry row referenced by operations in every
organization that happens to run that workflow. A principal holding a broad
`workflow_scope` (e.g. `"*"`) and `environment_scope=["*"]` grant in **any single
organization** therefore had their workflow-scope pattern satisfied by **any**
operation anywhere in the database with a matching `workflow_id` — including
operations belonging to a completely unrelated organization, whenever that
organization happened to run a workflow with the same id.

This is exactly the same *class* of bug T-54 (`docs/THREAT_MODEL.md`) closed for
`core.authorization.evaluate`'s environment-scope check at stage 04 — a
wildcard/broad grant that fails to also check organization ownership of the
resource it's being checked against.

### The fix

`operation_clause`'s `EXISTS` subquery now also requires:

```python
Operation.environment_id == environment_id if environment_id else false()
```

mirroring `environment_clause`'s own existing `AuditLogEntry.subject_id ==
environment_id if environment_id else false()` pattern exactly. The `list_page`
docstring was updated to explain both the `"operation"` branch's new reasoning and
why the sibling `"workflow"` branch is *correctly* left unfiltered (workflow
*definitions* are global/org-agnostic by design — the same workflow-scope pattern
that authorizes seeing the shared registry entry authorizes seeing audit events
keyed directly on that entry's own id; see Part 3 below for confirmation this is a
deliberate design choice, not a second gap).

### Verification

- `git diff src/n8n_operator/storage/repository.py` (already in the working tree
  before this task started) reviewed line by line against the attack class it
  closes — confirmed correct and minimal.
- `tests/integration/test_v2_integrated_scenario.py::TestTwoOrgThreeEnvironmentScenario::test_get_metrics_and_audit_events_never_cross_the_org_boundary`
  — written by a prior task specifically to catch this bug, against real
  PostgreSQL — confirmed **failing** against the unfixed code (reproduced by
  `git stash` on `repository.py` alone and re-running) and **passing** against the
  fixed code.
- The new `tests/integration/test_tenant_isolation_matrix.py::test_v2_read_surface_never_crosses_the_org_boundary[list_audit_events]`
  was independently confirmed to fail the same way against the unfixed
  `repository.py` (same `git stash` technique) and pass against the fix — proving
  the new regression guard genuinely exercises this vulnerability class, not just a
  case that happens to pass either way.

### New test coverage added this task

**Repository level** (`tests/integration/test_metrics_audit_repository.py`):
- `test_audit_log_list_page_matrix_never_crosses_orgs_and_still_allows_same_org` —
  parameterized over wildcard (`%`), exact (`crm.shared`), and prefix (`crm.%`)
  `workflow_id_like_patterns`, in every combination across two organizations, three
  environments (org A: staging + production; org B: production), one shared
  workflow id. Asserts both negative (never crosses) *and* positive (own org's
  operation still visible) outcomes — a fix that only ever returned nothing would
  pass every negative-only test but fail this one.
- `test_audit_log_list_page_empty_workflow_scope_sees_nothing_in_either_org` — an
  empty (non-`None`) pattern list sees nothing, in either organization.
- `test_audit_log_list_page_pagination_cursor_reapplies_scope_on_every_page` —
  interleaved org A/org B rows, walking `before_seq` pages two at a time; confirms
  every page, not only the first, re-applies the scope filter.
- `test_audit_log_list_page_workflow_subject_detail_never_carries_tenant_data` — the
  global, workflow-scoped `subject_type="workflow"` branch's `detail` (mirroring the
  real `operation.prepare_denied` shape `core.service.prepare_operation` writes)
  contains only structural facts (byte counts, configured limits, an error code) —
  never an n8n identifier, credential reference, webhook path, or anything
  email-shaped.
- (Pre-existing, from the prior session)
  `test_audit_log_list_page_excludes_operation_outside_resolved_environment` — the
  original, narrower reproduction of the bug.

**Repository level, real PostgreSQL**
(`tests/integration/postgres/test_audit_log_cross_org_isolation.py`, new file):
same two-org/three-env/shared-workflow-id matrix (wildcard/exact/prefix), run
against a real, freshly migrated PostgreSQL database per `test_quorum_concurrency.py`'s
own pattern — the strongest evidence the fix isn't an artifact of SQLite's simpler
query planner.

**Service level** (`tests/integration/test_metrics_audit_service.py`), through the
real `_resolve_scope`/`identity.resolve_environment` authorization path (not the
repository directly):
- `test_list_audit_events_v2_never_leaks_an_operation_across_organizations`
- `test_get_metrics_v2_never_counts_another_organizations_operation` (Part 2's own
  regression test)
- `test_list_audit_events_v2_pagination_cursor_reapplies_scope_on_every_page`

**MCP contract level** (`tests/integration/test_mcp_metrics_audit_tools.py`), a full
`MCPServer.call_tool` round trip:
- `test_list_audit_events_never_leaks_another_orgs_operation_through_mcp` — proves
  the anti-enumeration property this codebase uses everywhere else
  (`WORKFLOW_NOT_FOUND` never distinguishing "doesn't exist" from "you can't see
  it"): another organization's operation is **absent** from the result, never
  present with a redacted placeholder.

**Audit event shapes beyond `"operation"`:** `audit/writer.py`'s callers use exactly
four `subject_type` values in this codebase — `"workflow"`, `"operation"`,
`"environment"`, `"registry_snapshot"`. Approval decisions, retry lineage, and
reconciliation annotations are **not** distinct `subject_type` values — they are all
written with `subject_type="operation"` (`action="approval.routed"`,
`action="operation.reconciliation_recorded"`, and the transition-audit writes T04
through T15 all go through `_apply_and_audit`, always `subject_type="operation"`).
There is no separate "notification" or "retry" `subject_type`. The
`subject_type="operation"` fix above therefore already covers every one of these
shapes; the new tests above additionally exercise `action="operation.prepared"` and
`action="operation.prepare_denied"` explicitly, and the pre-existing test suite
already covers `"environment"` and `"registry_snapshot"`.

## Part 2 — `get_metrics`: verified not vulnerable

**Claim to verify:** `core.service.get_metrics` filters on `Operation.environment`
(the legacy free-text idempotency-namespace column), not `Operation.environment_id`
via a separate unscoped subquery — and in v2 mode `prepare_operation`/`retry_operation`
populate `Operation.environment` with the *same resolved environment ULID* as
`Operation.environment_id` (`src/n8n_operator/core/service.py:1744-1750`, the
`environment_value` computation in `prepare_operation`). If true, `get_metrics`'s
scope filter (`storage.repository._operation_scope_clauses`, shared with
`OperationRepository.list`/`ExecutionResultRepository.list_finished_durations_ms`)
was always a correct per-row comparison, never the class of separate-subquery gap
`list_page` had.

**Verified true**, by source reading (`_operation_scope_clauses` in
`storage/repository.py` — `Operation.environment == environment`, a plain column
comparison, not a subquery) and by two real, executed regression tests:

- `tests/integration/test_v2_integrated_scenario.py::TestTwoOrgThreeEnvironmentScenario::test_get_metrics_and_audit_events_never_cross_the_org_boundary`
  (pre-existing, real PostgreSQL) — passes.
- `tests/integration/test_metrics_audit_service.py::test_get_metrics_v2_never_counts_another_organizations_operation`
  (new, this task) — two organizations, one shared workflow id, each organization
  prepares exactly one operation; org B's `get_metrics` totals count is asserted to
  be exactly `1`, never `2` — passes.
- `tests/integration/test_tenant_isolation_matrix.py::test_v2_read_surface_never_crosses_the_org_boundary[get_metrics]`
  (new, this task) — passes.

**Conclusion: `get_metrics` was never vulnerable to this bug class. No fix needed.**

## Part 3 — systematic audit of every other named v2 surface

Method: read the actual query/authorization code for each surface and determine
whether it scopes a resource **only** by `workflow_id`/`workflow_id_like_patterns`/
`principal_id` without **also** constraining by the caller's authorized
organization/environment on a per-row basis (the exact class T-54 and T-66 both
are). No speculative hardening was applied — only confirmed gaps would have been
fixed, and none beyond T-66 itself were found.

| Surface | Verdict | Reasoning |
|---|---|---|
| `operations` (`OperationRepository.list`, via `core.service.list_operations`) | **Not vulnerable** | Filters on `Operation.environment == resolved_environment` (the same resolved-ULID column `get_metrics` uses, Part 2) **and** `workflow_id_like_patterns` gathered only from memberships whose `environment_scope == ["*"]`. Confirmed by `tests/integration/test_tenant_isolation_matrix.py::[list_operations]`. |
| `approvals` (`get_approval_status`) | **Not vulnerable** | Single-operation read gated by `_get_owned_operation_row`, which resolves the operation's own `environment_id` to its owning `organization_id` and passes both into `_authorize`/`authorization.evaluate` — `_environment_scope_satisfied` refuses a membership whose own organization does not own that environment, *before* checking the scope pattern itself (this is T-54's own fix, reused here). A cross-org call raises `OperationNotFoundError`, the same shape a nonexistent operation raises (invariant I14). Confirmed by the matrix test's `[get_approval_status]` case. |
| `environments` (`list_environments`) | **Not vulnerable** | Built from `identity.list_visible_environments`, which enumerates only the caller's own active `OrganizationMembership` rows and lists each one's own organization's environments — there is no path to another organization's environment at all. Confirmed by the matrix test's `[list_environments]` case. |
| `notification deliveries` (`NotificationDeliveryRepository`) | **Not applicable** | No caller-facing read tool exists for this table at all — `get_by_idempotency_key`/`list_pending` are used only by internal dedup/sweep logic (`_deliver_with_dedup`, `retry_failed_notifications`), never exposed to a scoped v2 caller. Nothing to scope. |
| `execution results` (`get_execution_result`) | **Not vulnerable** | Same `_get_owned_operation_row` gate as `get_approval_status` above. Confirmed by the matrix test's `[get_execution_result]` case. |
| `retries` / `reconciliation` (`list_reconciliation_events`) | **Not vulnerable** | Same `_get_owned_operation_row` gate; reconciliation events are read via `AuditLogRepository.list_for_subject(subject_type="operation", subject_id=operation_id)` for a single, already-authorized `operation_id` — no scope query involved. Confirmed by the matrix test's `[list_reconciliation_events]` case. `reconcile_operation` (the write path) is gated even more strictly — `admin`-only, no ownership shortcut at all. |
| `diffs` (`diff_workflow_definition`) | **Not applicable, correct by design** | Workflow-scoped only, exactly like the audit log's own `subject_type="workflow"` branch — a workflow *definition* is global/org-agnostic (one registry, shared by every organization), so a workflow-scope-pattern check via `_apply_environment`/`_authorize` is the entire authorization surface; there is no operation-level data in a diff result to leak across an organization boundary. This is the intended design, not a gap — the same reasoning documented in `list_page`'s own updated docstring for its `"workflow"` branch. |
| `alerts` (`check_and_deliver_alerts`) | **Not applicable, correct by design** | A system-wide maintenance sweep with no caller/principal argument at all (mirrors `expire_overdue_operations`'s own "operator-level view across every principal" shape) — it scans every `EXECUTING`/`UNKNOWN` operation database-wide and delivers to an operator-configured sink, not to a tenant. There is no per-caller visibility distinction for this function to get wrong. |
| `anchors` (`AuditAnchorRepository`) | **Not applicable, correct by design, confirmed via ADR-012** | `get_anchor_status`/`get_latest_anchor` are gated by `_require_admin` (any organization's `admin` role, not organization-scoped further) and return only content-free summaries — `covers_through_seq`, `entry_hash`, timestamps, no operational detail whatsoever. ADR-012 section 2 states this explicitly: "Content-free anchors mean adding an anchor sink does not widen the data-exposure surface." Anchors cover the *whole* audit chain by design (the chain itself is one physical hash chain, not partitioned per organization) — this is the intended design, not a gap. |

**No additional vulnerabilities of this class were found beyond the T-66 finding
itself.** No speculative changes were made to any of the "not vulnerable"/"not
applicable" surfaces above.

### A related, out-of-scope observation (not fixed, noted for the record)

`OperationRepository.count_recent` (used by `prepare_operation`'s rate-limit check)
is deliberately **not** organization-scoped — its own docstring states "rate
limiting is a property of the *workflow*, not of one principal's own history."
Because the workflow registry is global, this means an organization's rate-limit
denial can, in principle, be influenced by another organization's request volume
against a shared workflow id, and the resulting `operation.prepare_denied` audit
entry's `recent_count` value (visible to any principal with a matching
workflow-scope grant, since it is a `subject_type="workflow"` event) reflects a
cross-organization aggregate count. This is a pre-existing, deliberate application
behavior (global per-workflow rate limiting), not an unauthorized-read bug of the
class this review targeted — fixing it would mean *redesigning* rate limiting to be
per-organization, which is new feature work, not a scoped security fix. Flagged
here for visibility; not fixed in this task per the "no new product feature" and "no
speculative hardening" constraints given for this review.

## Part 4 — the durable regression guard

`tests/integration/test_tenant_isolation_matrix.py` (new file) parameterizes one
two-organization/three-environment/shared-workflow-id fixture across seven v2 read
surfaces audited in Parts 1-3: `list_audit_events`, `get_metrics`,
`list_operations`, `get_execution_result`, `get_approval_status`,
`list_reconciliation_events`, `list_environments`. Each surface's own check function
asserts org B's own principal never sees org A's operation/environment data. A
future new v2 read surface belongs in the `SURFACES` list the moment it exists,
rather than needing its own bespoke cross-org test written from scratch — the same
kind of test-coverage gap that let T-66 go unnoticed until this review.

Confirmed the guard actually detects the T-66 vulnerability class: reverting
`repository.py`'s fix alone (`git stash` on that one file) makes
`test_v2_read_surface_never_crosses_the_org_boundary[list_audit_events]` fail with
an explicit assertion showing org A's operation id present in org B's own result;
restoring the fix makes it pass again.

## Part 5 — threat model

New entry: `docs/THREAT_MODEL.md` T-66, in section 5.5 (Transport and storage),
immediately after T-65 — status `mitigated`, citing every test file above.

## Test summary (real, executed)

All commands run from the repository root, `feat/v2-stage-11-integration-release-and-proof`.

```
uv run pytest tests/integration/test_metrics_audit_repository.py tests/integration/test_repository.py -q
  26 passed  (test_metrics_audit_repository.py alone)
  62 -> 88 passed combined (see full run below)

uv run pytest tests/integration/test_v2_integrated_scenario.py -q -k org_boundary  (real Postgres)
  1 passed

uv run pytest tests/integration/postgres/test_audit_log_cross_org_isolation.py -q  (real Postgres)
  3 passed

uv run pytest tests/integration/test_metrics_audit_service.py -q
  21 passed

uv run pytest tests/integration/test_mcp_metrics_audit_tools.py -q
  8 passed

uv run pytest tests/integration/test_tenant_isolation_matrix.py -q
  7 passed

uv run pytest tests/integration/ tests/contract/ -q  (SQLite; -m "not postgres" implied by
  the missing env var for the plain run, real Postgres run separately, see below)
```

Full suite results (final, after all fixes) are recorded in the commit this
document ships with; see the task-5 report
(`.superpowers/sdd/2026-08-30-stage-11-v2-integration-release-and-proof/task-5-report.md`)
for the complete, literal command output.

## Lint/type gates

Every new or modified file in this change passes:
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy --strict src/`

## Pre-existing, unrelated finding

`tests/contract/test_docs_consistency.py::test_documentation_is_internally_consistent`
fails on this branch **before this task's changes** (confirmed via `git stash`):
five `docs/evidence/stage11-*.md` files from earlier stage-11 tasks are missing from
the repository tree published in `BUILD_PLAN.md` section 4 (`D9`). This is unrelated
to the security review in this document and was not introduced, worsened, or fixed
by this task — noted here for visibility only.
