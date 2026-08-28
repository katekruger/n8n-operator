# ADR-017: Team approval quorum semantics

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-003](ADR-003-operation-handles.md), [ADR-010](ADR-010-approval-delivery-and-expiry.md), [ADR-012](ADR-012-governed-retry-and-audit-anchoring.md), [ADR-015](ADR-015-rbac-authorization-evaluation.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 5.4 (invariant I11), 9 (boundary B4), [THREAT_MODEL.md](../THREAT_MODEL.md) T-07, T-20

## Context

v1's `approvals` table holds exactly one decision per operation, made by the one human
who happened to see it. v2's outcome statement promises "an approval can require N
distinct human approvers and route to them" — which means the same table shape has to
grow into something that can hold *several* decisions per operation and know when
enough of them have arrived.

Three things break quietly if this is designed while `request_approval` is being built
rather than before: what happens to a quorum when the org's membership changes while a
decision is still pending; whether the same person who requested the operation can also
be one of the people who decides it; and whether a decider gets to change their mind or
decide twice.

## Decision

**The eligible-approver set is snapshotted at request time and never re-expanded. A
single rejection is final. A decision, once cast, is a fact — even if the decider is
later removed. The requester can never be an eligible approver for their own request.**

### 1. Quorum policy and the eligibility snapshot

`request_approval(operation_id, ...)` writes an `approval_policy_snapshot`: the exact
list of `principal_id`s who, at that moment, hold the `approver` role
([ADR-015](ADR-015-rbac-authorization-evaluation.md)) scoped to this operation's
workflow and environment — **excluding the operation's own preparing principal,
structurally, regardless of whether they hold `approver` elsewhere** (self-dealing,
closed at the data layer, not by a runtime check someone could bypass by calling in a
different order) — plus the required `quorum_count` (N). This mirrors
`definition_hash` being pinned at prepare time
([ADR-002](ADR-002-default-deny-registry.md)): the policy in force *right now* is what
gets evaluated, and later changes to the org's membership do not retroactively rewrite
what was requested.

**Quorum is never re-expanded after the snapshot.** A principal granted the `approver`
role *after* `request_approval` was called is not eligible for this operation — they can
decide on the next one. This is deliberate: silently admitting a newly-added approver
into an already-in-flight, potentially sensitive decision is exactly the kind of
authority a human did not explicitly grant for *this* request.

**A principal removed from the eligible set after deciding keeps their decision.** Once
cast, a decision is a historical fact the same way a completed audit entry is — nothing
retroactively invalidates it, matching BUILD_PLAN's append-only audit stance (section
9.4) extended to approval decisions specifically. A principal removed *before* deciding
simply can no longer decide; their slot in the snapshot is permanently unfillable by
them. Quorum is evaluated as `count(decisions cast by principals in the snapshot) >=
quorum_count` — removal shrinks who *can still* contribute, never the historical record
of who already did.

If removal leaves quorum structurally unreachable (too many of the snapshotted
approvers removed before deciding), the operation is not automatically resolved either
way — it remains `PENDING_APPROVAL` until its normal `approval_ttl_seconds` deadline
(invariant I9 already handles this: it expires like any other operation nobody acts on
in time). An `admin` who notices can `cancel_operation` and have the caller
`prepare_operation` fresh, which produces a new snapshot against current membership.
Nothing here adds a way to force quorum down after the fact — that would be exactly the
kind of policy relaxation ADR-016 already refuses to allow for environment overlays,
applied to approval instead.

### 2. One rejection is final; approvals must be unanimous within the snapshot

Quorum governs the **approve** path only: reaching `APPROVED` (T06) requires
`quorum_count` approve decisions from within the snapshot, with **zero** rejections. A
single reject from any snapshotted approver moves the operation to `REJECTED`
immediately — the same one-human-says-no-is-final shape T07 already has in v1, not
weakened by adding more possible deciders. This needs no new state and no "partially
rejected" concept: `REJECTED` already means what it needs to mean.

### 3. A decision is cast exactly once

A principal in the snapshot who has already decided (either direction) on this
operation cannot decide again. A second attempt is `APPROVAL_ALREADY_DECIDED` and
changes nothing — not a silently-ignored no-op, an explicit error, so a UI or CLI
surfacing it can tell the human clearly rather than behaving as if the second click did
something.

## Consequences

### Positive

- Self-dealing is closed by construction — the requester is never in the snapshot, so
  there is no runtime check to accidentally skip.
- Snapshotting eligibility at request time gives every quorum decision the same
  "evaluated against the policy in force when it mattered" property v1 already gives
  drift detection and idempotency — a familiar shape reused, not a new kind of
  time-sensitivity introduced just for approvals.
- Fail-fast rejection means an organization never needs a "someone objected but we
  reached quorum anyway" conversation — the product cannot produce that state.
- `APPROVAL_ALREADY_DECIDED` as an explicit error, rather than silent idempotent
  success, keeps a double-click from being misread as "my first click didn't register,"
  which is the more common real failure mode worth surfacing clearly.

### Negative

- An operation can become permanently unreachable-by-quorum through membership churn
  without ever being explicitly resolved — it just sits `PENDING_APPROVAL` until its TTL.
  An organization with high approver turnover and long TTLs may see more of these than
  expected. Mitigated by keeping `approval_ttl_seconds` reasonably short by policy, not
  by adding automatic re-snapshotting (which would reopen the retroactive-eligibility
  problem this ADR exists to close).
- No mechanism exists for an approver to *delegate* their pending decision to another
  approver — if they are unavailable, the operation waits for someone else in the
  snapshot or expires. Delegation is a real team-approval feature many products have;
  deliberately deferred rather than designed under this stage's time pressure.
- The requester-exclusion rule means a very small organization (one admin, one
  approver, both the same overworked person on `operator` and `approver` roles for
  convenience) can find themselves unable to approve their *own* prepared operations at
  all if no second approver exists — which is the entire point of a quorum system, but
  is worth stating plainly as a real operational consequence an organization must
  provision for (at least two people holding `approver` for anything that needs
  quorum > 0), not a hidden trap.

## Alternatives considered

**Re-expand the eligible-approver snapshot when new approvers are granted while an
operation is still pending.** Rejected in section 1: silently admitting a newly-added
approver into an already-in-flight decision grants authority over a specific request
that no human explicitly decided to grant — the org admin who added the approver was
managing membership, not routing this operation.

**Auto-resolve an operation (approve or reject) when membership churn makes quorum
structurally unreachable.** Rejected: automatically rejecting would let membership
administration — an unrelated act — silently kill a legitimate pending request; automatically
approving is obviously worse. Falling through to the existing TTL expiry (invariant I9)
reuses a mechanism that already exists rather than inventing a new resolution path for
one edge case.

**Allow a rejected decision to be reversed, or allow re-voting once quorum is
reached.** Rejected: it would mean `REJECTED` and `APPROVED` are not really terminal
outcomes of the decision process, undermining the same one-human-says-no-is-final
property v1 already relies on for its single-approver `REJECTED` transition (T07).

**Silent idempotent success on a second decision from the same approver, rather than
`APPROVAL_ALREADY_DECIDED`.** Rejected: a double-click silently doing nothing is
indistinguishable, from the approver's side, from a double-click that worked — exactly
the ambiguity an explicit error exists to remove.

**Approval delegation, letting an unavailable approver hand their pending decision to
someone else.** Rejected for this stage: a real feature many team-approval products
have, but designing it under this stage's contract-closure pressure risks getting the
delegation-authority question (who may delegate to whom, and does that need its own
approval) wrong. Deliberately deferred rather than improvised.
