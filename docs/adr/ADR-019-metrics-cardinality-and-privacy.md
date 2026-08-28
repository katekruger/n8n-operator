# ADR-019: Metrics cardinality and privacy

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-002](ADR-002-default-deny-registry.md), [ADR-015](ADR-015-rbac-authorization-evaluation.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 9 (boundary B5), [THREAT_MODEL.md](../THREAT_MODEL.md) T-10

## Context

`get_metrics` is the one v2 tool whose entire purpose is to aggregate across many
operations — which is exactly the shape a naive implementation turns into an
enumeration or de-anonymization surface without meaning to. Three concrete failure
modes are easy to introduce here: a breakdown that includes workflows the caller is not
authorized to see (T-10 again, this time via aggregation rather than a direct lookup); a
breakdown with unbounded cardinality that a caller can mine one small request at a time
to enumerate workflow IDs; and a "p99 latency" computed from a single sample, which is
not a percentile at all — it is that one operation's exact duration, disclosed under a
statistical-sounding label.

## Decision

**Metrics are pre-filtered to the caller's authorized workflow set before aggregation,
bounded to enumerated time windows and a capped number of breakdown entries, and never
report a percentile computed from fewer than ten samples.**

### 1. Authorization filters before aggregation, not after

`get_metrics` computes every breakdown (by workflow, by risk class, by side-effect
class, by outcome) only over operations the caller is authorized to see under
[ADR-015](ADR-015-rbac-authorization-evaluation.md)'s workflow/environment scope
intersection — applied as a filter on the underlying query, before any count or
percentile is computed, not as a post-hoc redaction of an already-aggregated result. An
unauthorized workflow's operations do not appear in any total, count, or percentile the
caller receives — not merged into an "other" bucket, not present in an aggregate total
at all, because even a caller correctly inferring "the org-wide total is higher than
what I can see" from a labeled subtotal is not new information (organization membership
already implies other members exist) — but a *workflow-scoped* aggregate leaking would
be exactly the T-10 shape.

### 2. Bounded cardinality on any grouping dimension

A single `get_metrics` response returns breakdown entries for at most **50** distinct
values on any one dimension (workflow ID being the dimension most likely to approach
that bound in a large organization). Beyond 50, additional entries fold into a single
`"other"` bucket carrying only an aggregate count, never individual identifiers. This
bounds both response size and the "many small requests reveal one more workflow ID
each" enumeration shape a truly unbounded breakdown would permit — 50 is generous
enough to be useless as a rationing mechanism for a legitimately-sized team, and small
enough that fishing for the next unseen workflow ID is not a practical strategy.

### 3. Enumerated windows only

`get_metrics` accepts a `window` argument from a fixed enum: `1h`, `24h`, `7d`, `30d`.
No caller-supplied arbitrary start/end range exists in v2. This bounds query cost
predictably and closes a narrower but real version of the same problem section 2
addresses: an arbitrarily narrow custom window (e.g. a 10-second window around a known
event) could otherwise be used to isolate and de-anonymize a single operation's metrics
under the guise of a legitimate aggregate query.

### 4. Percentiles require a minimum sample size

A latency percentile (p50, p95, p99) for any bucket is returned only when that bucket
has **at least 10** samples within the requested window. Below that threshold, the
field is `null` with `"reason": "insufficient_sample"` rather than a number. A "p99"
computed from one or two samples is not a percentile — it is that operation's exact
duration wearing a statistical label, and for a low-volume or newly-registered workflow
this is a realistic way to identify one specific execution's timing (which, combined
with an external observation of when a downstream side effect occurred, can narrow down
a lot). Ten is a deliberately round, defensible floor: high enough that no single
sample dominates a percentile's shape, low enough that a genuinely active workflow
reports real numbers quickly.

## Consequences

### Positive

- Filtering before aggregation means `get_metrics`' authorization story is identical in
  shape to every other v2 tool's — one rule, applied consistently, rather than a special
  "aggregation is different" carve-out that would need its own review every time the
  metrics query changes.
- The 50-entry cap and enumerated windows both bound response size and cost as
  side effects of closing an information-disclosure gap, not as a separate performance
  decision layered on afterward.
- The sample-size floor means every percentile a caller receives is actually meaningful
  as a percentile, not just non-disclosive — a correctness improvement that happens to
  also be the privacy fix.

### Negative

- A newly-registered or genuinely low-volume workflow may show `null` latency
  percentiles indefinitely if it never crosses 10 executions within any single window —
  an organization monitoring a rarely-run but important workflow gets less visibility
  into its performance than a high-volume one. Accepted: the alternative is disclosing
  individual operation timings under a percentile label.
- The `"other"` bucket at 50+ distinct values means a very large organization loses
  per-workflow granularity in its highest-level view once it outgrows the cap — they can
  still get per-workflow detail by narrowing the query with an explicit workflow-scope
  filter (a single named workflow is one entry, never bucketed), just not in one
  unfiltered organization-wide call.
- Enumerated windows mean a caller who genuinely needs a custom range (e.g. "the exact
  hour of an incident, which happens to span a window boundary") cannot get it from
  `get_metrics` directly — they fall back to `list_audit_events`'s own pagination over
  the raw event stream for that kind of forensic query, which is the more appropriate
  tool for a bounded, specific investigation anyway.

## Alternatives considered

**Aggregate first, then filter out unauthorized workflows from the computed result.**
Rejected in section 1: an aggregate total computed across authorized and unauthorized
operations alike, then merely re-labeled or trimmed, can still leak information through
the aggregate itself (a caller comparing a labeled subtotal against a suspiciously
larger implied total). Filtering the underlying query before any count or percentile
runs is the only way the caller never receives a number influenced by data they
cannot see.

**Unbounded breakdown cardinality, relying on response-size limits alone to bound
cost.** Rejected: cost was never the primary problem — cardinality here is an
enumeration surface (each additional visible entry is one more workflow ID confirmed to
exist), and a size limit alone does not stop many small requests from each revealing one
new entry. The 50-entry cap plus `"other"` bucket closes the enumeration angle directly.

**Caller-supplied arbitrary start/end timestamps instead of enumerated windows.**
Rejected in section 3: an arbitrarily narrow custom window can isolate a single
operation's metrics under the guise of a legitimate aggregate query — a narrower version
of the same de-anonymization risk unbounded cardinality poses. Enumerated windows are a
small ergonomic cost for closing a real disclosure path.

**No minimum sample size — report whatever percentile the data supports, even from one
sample.** Rejected in section 4: a "p99" from one sample is not a percentile, it is that
operation's exact duration under a statistical-sounding label, and for a low-volume
workflow that is a realistic way to identify one specific execution's timing.
