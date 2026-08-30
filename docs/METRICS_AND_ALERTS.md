# Metrics and alerts

Stage 08 gives GTM teams operational visibility without a dashboard: `get_metrics` (an
authorization-filtered aggregate query), `list_audit_events` (a scoped audit-trail
query), and three alert-hook triggers delivered over the same `NotificationSink` stage
05 built for approval routing ([ADR-018](adr/ADR-018-notification-and-alert-hook-delivery.md)).

## `get_metrics`

```bash
n8n-operator metrics show [--environment ENV] [--window 1h|24h|7d|30d] \
  [--group-by workflow|risk|side_effects|outcome] [--json]
```

**Denominator**: `totals.count` is every operation created within the window, scoped to
the caller's own authorized workflows (v2) — the same filter-before-aggregate rule
[ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md) requires, so a marketing
viewer's totals never include a sales-only workflow's operations, not even folded into
an anonymous count.

**Window**: one of `1h`/`24h`/`7d`/`30d`, always relative to `generated_at` (UTC) —
there is no caller-supplied arbitrary start/end range, deliberately (an arbitrarily
narrow window could isolate a single operation).

**Timezone**: everything is UTC. `generated_at` and every timestamp anywhere in this
system is RFC 3339 UTC; there is no per-caller timezone conversion. Convert on your own
side if you need local time.

**Percentile floor**: `latency_ms.p50`/`p95`/`p99` are computed only when their own
bucket has at least 10 finished-execution samples in the window. Below that, the field
is `null` and its sibling `p50_reason`/`p95_reason`/`p99_reason` reads
`"insufficient_sample"` — never a number computed from too few points to mean anything.

**Empty-set behavior**: an empty window (zero operations) returns `totals.count: 0`,
`totals.by_outcome: {}`, and every percentile `null` with `"insufficient_sample"` — not
an error.

**Cardinality cap**: `group_by=workflow` returns at most 50 distinct workflow entries,
sorted by count descending; anything beyond that folds into one `"other"` entry
carrying only an aggregate count and a note of how many workflows were folded — never
individual identifiers. Cardinality here is bounded by the registry's own distinct
workflow count (a caller can create operations, never a new workflow id), so this cap
exists for response-size hygiene, not as a defense against an attacker inflating it.

## `list_audit_events`

```bash
n8n-operator audit list [--environment ENV] [--workflow-id ID] [--since RFC3339] \
  [--limit 1-100] [--cursor CURSOR] [--json]
```

Cursor-paginated, anchored to `audit_log.seq` — never an offset, which would silently
skip or duplicate rows over a concurrently-growing append-only log
([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) section 3). Authorization
filters the *query*, not the result: an event whose subject resolves to a workflow or
environment outside the caller's scope is excluded entirely, never returned with a
redacted `detail` — even the existence of an event for an unauthorized workflow is
enumeration-adjacent information the caller has no standing to receive. A
`registry_snapshot` event (a whole-registry-document event with no single
workflow/environment owner, e.g. `registry reload`) is visible to an `admin` caller
only.

`detail` carries the same write-time redaction v1 already applies — this adds no
second redaction pass and no broader-role view of any entry's raw content.

## Alert hooks

Three trigger conditions, delivered as `NotificationEvent`s through the configured
`NotificationSink` (webhook or local), deduplicated permanently by
`(subject_id, principal_id, event_type)` — an already-delivered alert is never
re-delivered, even across many sweep runs (ADR-018 section 2).

| Trigger | `event_type` | Mechanism | Dedup key covers |
|---|---|---|---|
| Definition drift blocks a `prepare`/`retry` | `drift.detected` | Reactive — fired the instant `prepare_operation`/`retry_operation` themselves discover `DEFINITION_DRIFT` | `workflow_id:live_hash` — the *same* drift persisting across many blocked attempts alerts once; the workflow drifting *again* to a different definition alerts again |
| An `EXECUTING` operation sits unchanged past a threshold | `operation.stuck` | Periodic sweep: `n8n-operator notifications check-alerts` | `operation_id` — alerts once per stuck operation, ever |
| An operation reaches `UNKNOWN` | `operation.unknown` | Periodic sweep: `n8n-operator notifications check-alerts` | `operation_id` — alerts once per operation, ever |

Run the sweep on a schedule (cron, a systemd timer) — it is idempotent and safe to run
as often as you like:

```bash
n8n-operator notifications check-alerts [--executing-stuck-threshold-seconds 3600]
```

**What this stage deliberately does not add**: database-health and
notification-delivery-failure alerts (both already independently observable today via
`n8n-operator db status` and the `notification_deliveries` table itself, without a
dedicated push alert), and audit-anchor-failure alerting (the underlying
`audit_anchors` publication mechanism does not exist until
[stage 09](BUILD_PLAN.md)). These may be revisited once that infrastructure lands.

## Starter thresholds — examples, not universal SLOs

The numbers below are the code's own defaults and reasonable starting points for a
small team, **not** a recommendation for your specific workload. Tune them against your
own operation volume and risk tolerance.

| Signal | Starter threshold | Why this number, loosely |
|---|---|---|
| `EXECUTING` stuck | 1 hour (`--executing-stuck-threshold-seconds 3600`) | Most n8n workflows finish in seconds to minutes; an hour is generous enough to avoid false positives from a genuinely slow workflow while still catching a truly hung dispatch same-day. |
| Sweep cadence | Every 5-15 minutes | Frequent enough that a stuck operation or a drift alert surfaces within one work session, infrequent enough not to spam a webhook endpoint. |
| Failure-rate review | Check `metrics show --window 1h` when `by_outcome.failed` exceeds ~10% of `count` | Not enforced anywhere in code — a human-judgment trigger for when to look closer, not an automated alert. |
| Approval backlog | Check `audit list --workflow-id <id>` for `PENDING_APPROVAL` operations approaching their `approval_expires_at` | Also not automated in this stage; `get_operation`'s own `approval_expires_at` field is the source of truth if you want to build your own reminder around it. |

If any of these don't fit your deployment, change them — they are defaults, not
contracts.
