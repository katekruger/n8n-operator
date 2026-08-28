# Stage 08 prompt — metrics, scoped audit query, and alert hooks

Copy this entire file into a fresh Claude Code session after Stages 06 and 07 are merged.

## Mission

Give GTM teams actionable operational visibility without building a dashboard or leaking
customer data and hidden workflow names.

## Required work

- Implement `get_metrics` for bounded windows and dimensions: operation counts, outcome
  distribution, approval wait, execution latency percentiles, drift counts, blocked
  preflights, retries, unknown outcomes, notification failures, and anchor health where
  available. Specify denominator, window, timezone, empty-set, and percentile behavior.
- Implement `list_audit_events` with organization/environment/workflow/action/outcome/time
  filters, stable opaque cursor pagination, strict limits, projection allowlists, and RBAC
  filtering at query time. Preserve append-only storage.
- Use bounded-cardinality labels. Never place email addresses, arguments, operation IDs,
  execution IDs, free-text reasons, error messages, or raw workflow IDs into metric labels.
- Add a monitoring port and an operator-friendly standards-based metrics exporter if Stage
  00 selected one. Metrics endpoints require appropriate network/auth controls.
- Add alert hooks for high-value conditions: sustained failure rate, approval backlog,
  repeated drift, `UNKNOWN`, notification delivery failure, database health, and audit-anchor
  failure. Hooks carry minimal redacted content, authenticate, deduplicate, apply bounded
  delivery retries, and never trigger workflow execution.
- Add CLI summaries and practical runbooks with suggested starter thresholds that are
  clearly examples, not universal SLOs.

## GTM scenarios to prove

- Detect enrichment failures after a vendor/API change.
- Identify production CRM operations blocked by definition drift.
- See campaign approvals aging toward expiry.
- Compare staging versus production outcomes without exposing sales-only workflows to a
  marketing-only viewer.
- Trace a failed operation from a metric to authorized audit evidence.

## Required edge cases

Sparse samples, zero denominators, high-cardinality attacks, cross-tenant aggregation,
cursor tampering, records inserted during pagination, daylight-saving boundaries, long
windows, slow queries, exporter scraping during migration, alert storms, sink outage,
duplicate alerts, redaction failure, and permissions revoked between pages.

## Completion gate

Load-test representative operation volumes, inspect query plans, enforce response and query
limits, test negative authorization, and verify no metric/audit result leaks seeded secrets
or hidden identifiers. Return Stage 09 entry criteria.
