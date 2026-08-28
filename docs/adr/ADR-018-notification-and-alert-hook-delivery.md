# ADR-018: Notification and alert-hook delivery guarantees

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-005](ADR-005-no-automatic-retry-v1.md), [ADR-012](ADR-012-governed-retry-and-audit-anchoring.md), [ADR-017](ADR-017-team-approval-quorum-semantics.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 9 (boundaries B5, B6)

## Context

v2 has two outbound-delivery surfaces that did not exist in v1: routing a pending
operation to approvers (`request_approval`) needs to actually reach them, and
monitoring (`get_metrics`, drift, stuck operations) needs an alerting hook so a team
does not have to poll. Both are, mechanically, the same problem — deliver an event to
an external endpoint Operator does not control — and left unspecified independently
they would likely grow two different retry/dedup/content policies for no principled
reason.

The one thing that must **not** happen is confusing this with ADR-005's no-retry rule.
ADR-005 is about never retrying a *dispatch to n8n* — a side-effecting action against
Zone D — automatically, because a retried side effect might duplicate it. Notifying a
human that something needs their attention is not a side effect against Zone D at all;
retrying a failed notification carries none of that risk, and refusing to retry it
would just mean approvers sometimes silently never hear about a pending operation.

## Decision

**One `NotificationSink` interface serves both approval routing and alert hooks.
Delivery is at-least-once with dedup by idempotency key, best-effort ordering, and
notification payloads carry no operation content — only enough to fetch the real detail
through an authenticated channel.**

### 1. One interface, two event sources

```
NotificationSink
  deliver(event: NotificationEvent) -> DeliveryReceipt
```

`request_approval` and the alert-hook triggers (drift detected, an `EXECUTING`
operation stuck past a threshold, an operation reaching `UNKNOWN`) both call the same
interface with different `NotificationEvent` payloads. This is the same shape ADR-012
already established for `AuditAnchor` — one interface, several implementations, no
event source gets its own bespoke delivery code path. v2 ships one implementation: an
authenticated HTTPS webhook (TLS, a shared secret or bearer token in the request,
receipts retained) — the same mechanism family ADR-012 uses for the audit-anchor
webhook, reused rather than reinvented.

### 2. At-least-once, deduplicated, retried, bounded

Every delivery carries an idempotency key: `(subject_id, principal_id, event_type)` —
for approval routing, `subject_id` is the `operation_id` and `principal_id` is the
notified approver; for an alert hook, `subject_id` is whatever the alert concerns
(an operation ID, a drift finding) and `principal_id` is absent (alert hooks target a
configured endpoint, not a specific principal). A failed delivery is retried with
backoff, **bounded** — a fixed maximum attempt count, after which the failure is
recorded as `DELIVERY_FAILED` (fail-visible, never fail-silent, the same posture
ADR-012 requires of anchor publication) rather than retried forever. The dedup key
means a retry that eventually succeeds, or a delivery attempted twice by an operator
error, never produces two notifications for the same event to the same recipient.

This bounded-retry behavior is unambiguously **not** the same rule ADR-005 states for
n8n dispatch. Nothing here retries a workflow *execution*; nothing here touches Zone D.
An approver hearing about a pending operation twice because of a retried webhook is a
minor annoyance; a workflow dispatched twice because of a retried webhook would be a
duplicated side effect. The two are not the same risk and do not get the same rule.

### 3. Ordering is best-effort, not guaranteed

Notifications may arrive out of order relative to each other (retries and network
timing can reorder independent deliveries). This is acceptable because every
notification is self-contained and idempotent by key — nothing about correctly handling
one depends on having already seen another in sequence. v2 does not build a delivery
queue with ordering guarantees; that is meaningfully more infrastructure than the
outcome requires.

### 4. Notification payloads carry no operation content

A notification body contains: the event type, `operation_id` (or the relevant subject
ID for an alert), a timestamp, and instructions/a reference for fetching full detail
through an **authenticated** channel (the CLI, or a v2 API call subject to the same
RBAC evaluation as everything else, [ADR-015](ADR-015-rbac-authorization-evaluation.md)).
It never contains the operation's arguments, the workflow's title or description, or
any redacted-or-not execution result. This extends boundaries B5/B6's "no credential,
no unredacted data leaves the process" discipline to a surface those boundaries did not
originally anticipate: a third-party notification channel (an organization's own
webhook receiver, potentially forwarding into Slack or email infrastructure Operator
does not control) is not a place operation content should land just because it is
convenient to include. A human who wants to see what they are approving reaches for the
same CLI or approval-app surface v1 already uses for that ([ADR-010](ADR-010-approval-delivery-and-expiry.md)).

## Consequences

### Positive

- One interface and one set of guarantees means stage 05 (approvals) and stage 08
  (metrics/alerts) build against the same contract instead of inventing two, and a
  future third event source (v3) has an obvious place to plug in.
- Bounded retry with dedup means "did the approver get notified" has one honest answer
  per event, recorded, rather than an unbounded number of possible duplicate sends or a
  silently-swallowed single failure.
- Keeping content out of notification payloads means adding a notification sink to an
  organization's infrastructure never becomes a second place operation arguments or PII
  can leak from, regardless of how well-secured that third-party endpoint turns out to
  be in practice.

### Negative

- An approver has to leave the notification channel to see what they are actually being
  asked to approve — one extra step versus a notification that includes full context,
  accepted because the alternative widens the data-exposure surface in a way this
  product's whole design otherwise avoids.
- Bounded retry means a webhook endpoint down for longer than the retry window's total
  span genuinely never gets that notification — recorded as `DELIVERY_FAILED`, visible
  in the org's admin surface, but not retried indefinitely. An organization depending on
  eventual delivery for a long-outage endpoint needs its own out-of-band monitoring of
  `DELIVERY_FAILED` records; Operator does not compensate for a chronically unreliable
  receiver.
- Best-effort ordering means a downstream system that assumes notifications arrive in
  event order will occasionally be surprised. Any consumer building on this interface
  must be told plainly (in its own integration docs, stage 08) to treat each
  notification as independent.

## Alternatives considered

**Two separate interfaces — one for approval routing, one for alert hooks.** Rejected:
both are mechanically the same problem (deliver an event to an endpoint Operator does
not control), and building them separately risked exactly what Context warns about —
two different retry/dedup/content policies with no principled reason for the
difference. One interface with one set of guarantees is easier to reason about and to
extend in stage 08.

**Apply ADR-005's no-automatic-retry rule to notification delivery, on the theory that
"no automatic retries" should be a blanket product stance.** Rejected in section 2:
ADR-005 is about never retrying a side-effecting *dispatch to n8n* automatically,
because a retried dispatch might duplicate a real-world action. Retrying a failed
notification carries none of that risk — the dedup key means a retry that eventually
succeeds never produces two notifications for the same event, so the two rules govern
different risks and must not be conflated into one.

**Unbounded retry until delivery succeeds.** Rejected: an endpoint down for an extended
period would accumulate an unbounded retry backlog with no visible failure state.
Bounded retry with a `DELIVERY_FAILED` record is fail-visible; unbounded retry is
fail-silent-until-eventually-visible, which is the posture ADR-012 already rejected for
audit-anchor publication.

**Include full operation content (arguments, workflow title) in the notification body,
so an approver can act without an extra step.** Rejected in section 4: it would make
every configured notification sink — including a third-party webhook receiver Operator
does not control — a second place operation content could leak from, for a convenience
that a single authenticated fetch already provides.

**A delivery queue with strict ordering guarantees.** Rejected: meaningfully more
infrastructure than any v2 outcome requires, and every notification is already
self-contained and idempotent by key — nothing about handling one correctly depends on
having seen another first.
