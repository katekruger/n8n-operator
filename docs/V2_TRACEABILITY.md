# v2 traceability matrix

Maps every v2 outcome and tool to its acceptance criteria, tests, documentation, and
implementing stage. Written at stage 00 (contract closure) as the deliverable named in
that stage's spec; kept current by whoever implements each stage — a stage is not done
until its row's Tests column names real, passing tests, not planned ones.

**Status legend:** `contract` — specified in this stage, no runtime behavior yet.
`planned` — test file does not exist yet; named here so the implementing stage knows
exactly what to write. `done` — implemented and passing.

## Tools

| Tool | Acceptance criteria | Tests | Documentation | Stage | Status |
|---|---|---|---|---|---|
| `whoami` | AC-34, AC-35, AC-36, AC-45, AC-46 | `tests/unit/test_whoami.py`, `tests/integration/test_oidc_auth.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.1, [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md), [ADR-014](adr/ADR-014-oidc-trust-and-session-model.md) | 02 | contract |
| `list_environments` | AC-37, AC-47 | `tests/unit/test_environments.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.2, [ADR-016](adr/ADR-016-environment-registry-overlays.md) | 04 | contract |
| `request_approval` | AC-40, AC-41, AC-49 | `tests/unit/test_quorum_approval.py`, `tests/property/test_approval_snapshot.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.3, [ADR-017](adr/ADR-017-team-approval-quorum-semantics.md), [ADR-018](adr/ADR-018-notification-and-alert-hook-delivery.md) | 05 | contract |
| `get_approval_status` | AC-40, AC-49 | `tests/unit/test_quorum_approval.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.4, [ADR-017](adr/ADR-017-team-approval-quorum-semantics.md) | 05 | contract |
| `retry_operation` | AC-50 | `tests/unit/test_retry.py`, `tests/property/test_retry_no_reuse.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.5, [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) §1, invariant I11 | 06 | contract |
| `diff_workflow_definition` | AC-44 | `tests/unit/test_diff.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.6, [ADR-008](adr/ADR-008-conservative-definition-canonicalization.md) | 07 | contract |
| `get_metrics` | AC-42, AC-44 | `tests/unit/test_metrics.py`, `tests/property/test_metrics_privacy.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.7, [ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md) | 08 | contract |
| `list_audit_events` | AC-43, AC-44 | `tests/unit/test_audit_query.py` (planned) | [MCP_TOOLS.md](MCP_TOOLS.md) §5.8, [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) §3 | 08 | contract |
| Every v1 tool's v2 form (`environment` argument, result fields, pagination, authorization filtering, new errors) | AC-37, AC-38, AC-39, AC-44 | `tests/contract/test_mcp_tool_inventory.py` (existing, still passes: 12 tools unchanged in v1), per-tool additions planned per stage | [MCP_TOOLS.md](MCP_TOOLS.md) §5.9 | 02–04 | contract |

## Cross-cutting outcomes

| Outcome | Acceptance criteria | Tests | Documentation | Stage | Status |
|---|---|---|---|---|---|
| Organization membership and isolation model | AC-34 | `tests/unit/test_organizations.py` (planned) | [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md), BUILD_PLAN §8.3 | 02 | contract |
| Human vs. service principal semantics | AC-35 | `tests/unit/test_principal_kinds.py` (planned) | [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md) §2 | 02 | contract |
| Multi-org OIDC subject / active-organization selection | AC-36 | `tests/integration/test_oidc_auth.py` (planned) | [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md) §3 | 02 | contract |
| OIDC trust: key rotation, clock skew, disabled/removed re-check | AC-45, AC-46 | `tests/integration/test_oidc_auth.py`, `tests/property/test_disabled_principal.py` (planned) | [ADR-014](adr/ADR-014-oidc-trust-and-session-model.md) | 02 | contract |
| RBAC role-capability matrix, workflow×environment intersection | AC-38, AC-39, AC-44 | `tests/property/test_rbac_matrix.py` (planned) | [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md) | 03 | contract |
| No-enumeration-oracle extension (no `FORBIDDEN`, denial == absence) | AC-34, AC-44 | `tests/property/test_no_enumeration.py` (planned) | invariant I14 (BUILD_PLAN §5.5), [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md) | 03 | contract |
| Default environment resolution, production never implicit | AC-37 | `tests/unit/test_environments.py` (planned) | [ADR-016](adr/ADR-016-environment-registry-overlays.md) §3, rule R13 | 04 | contract |
| Environment overlays: field allowlist, strengthen-only, conflict prevention | AC-48 | `tests/unit/test_overlays.py` (planned) | [ADR-016](adr/ADR-016-environment-registry-overlays.md) §1, rule R14 | 04 | contract |
| Environment archival with historical-operation resolvability | AC-47 | `tests/unit/test_environments.py` (planned) | [ADR-016](adr/ADR-016-environment-registry-overlays.md) §4 | 04 | contract |
| Quorum snapshot immutability under membership churn | AC-40 | `tests/property/test_approval_snapshot.py` (planned) | invariant I13, [ADR-017](adr/ADR-017-team-approval-quorum-semantics.md) §1 | 05 | contract |
| Approval self-dealing exclusion, duplicate-decision rejection | AC-49 | `tests/unit/test_quorum_approval.py` (planned) | [ADR-017](adr/ADR-017-team-approval-quorum-semantics.md) §1, §3 | 05 | contract |
| Notification delivery: at-least-once, dedup, bounded retry, content-free | AC-41 | `tests/unit/test_notification_sink.py` (planned) | [ADR-018](adr/ADR-018-notification-and-alert-hook-delivery.md) | 05, 08 | contract |
| Governed retry: recalculation, no approval reuse, `UNKNOWN`-parent retry, concurrent-retry race | AC-50 | `tests/property/test_retry_no_reuse.py`, `tests/unit/test_retry_concurrency.py` (planned) | [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) §1, invariant I11 | 06 | contract |
| Metrics: authorization-before-aggregation, cardinality cap, enumerated windows, percentile floor | AC-42, AC-44 | `tests/property/test_metrics_privacy.py` (planned) | [ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md) | 08 | contract |
| Audit query: cursor pagination, authorization-filters-the-query, unchanged redaction | AC-43, AC-44 | `tests/unit/test_audit_query.py` (planned) | [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) §3 | 08 | contract |
| `AuditAnchor` implementations (signed local file, authenticated HTTPS webhook) | — (ADR-012 §2, no dedicated AC; covered by existing audit-integrity tests extended) | `tests/unit/test_audit_anchor.py` (planned) | [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) §2 | 09 | contract |
| v2 data-model additions (7 new/changed tables) | — (structural; exercised indirectly by every AC above) | `tests/contract/test_portable_sql.py` (existing, extend), Alembic autogenerate-empty-diff check (planned) | BUILD_PLAN §8.3 | 01, then per-table stage | contract |

## Documentation-drift contract tests (this stage's own deliverable)

These exist so a future stage cannot silently drift from what this document commits
to; they check documentation shape, not runtime behavior (this stage adds none).

| Check | Test | What it catches |
|---|---|---|
| v2 tool inventory matches BUILD_PLAN §7.2 and MCP_TOOLS.md §5 headings | `scripts/check_docs_consistency.py` D13 (new), run via `tests/contract/test_docs_consistency.py` | A tool renamed, added, or removed in one document but not the other. |
| AC range is exact (AC-01..AC-50, no gaps, no duplicates) | `check_docs_consistency.py` D6 (range extended) | A stage adding criteria without updating the checker's expected range. |
| Invariant / boundary / registry-rule ranges are exact (I1–I14, B1–B17, R1–R14) | `check_docs_consistency.py` D7 (ranges extended) | The same drift for invariants, boundaries, and registry rules. |
| Every 6+-char ALL-CAPS backticked code in MCP_TOOLS.md is taxonomy-registered | `check_docs_consistency.py` D11 (unchanged logic, new codes covered) | A new v2 error code introduced without a taxonomy row (`ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED`, `ENVIRONMENT_ARCHIVED`, `APPROVER_NOT_IN_POLICY`, `RETRY_NOT_APPLICABLE`). |
| ADR-013..019 structurally complete, referenced, not orphaned | `check_docs_consistency.py` D12 (ADRS list extended) | A new ADR missing a required section, or never cross-referenced from BUILD_PLAN/MCP_TOOLS. |
| Exactly 12 v1 tools still registered; no unplanned tool | `tests/contract/test_mcp_tool_inventory.py` (existing, unmodified) | This stage accidentally adding runtime tools — it must not. |
