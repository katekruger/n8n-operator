# ADR-012: Governed retry and external audit anchoring

- **Status:** Accepted
- **Date:** 2026-08-26
- **Updated:** 2026-08-28 (added section 3, `list_audit_events` query semantics, at v2 stage 00)
- **Deciders:** Lead architect
- **Phase:** 0.1 (architecture-decision closure), implemented in phase 10 (v2)
- **Related:** [ADR-005](ADR-005-no-automatic-retry-v1.md), [ADR-003](ADR-003-operation-handles.md), [ADR-015](ADR-015-rbac-authorization-evaluation.md), [ADR-019](ADR-019-metrics-cardinality-and-privacy.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 5.4, 9.4, [THREAT_MODEL.md](../THREAT_MODEL.md) T-35, RR-4, RR-5

## Context

Two v2 commitments were named in phase 0 without being specified, and both are the kind of
thing that goes wrong if it is designed while being built.

**Governed retry.** ADR-005 forbids automatic retries and promises v2 a `retry_operation`
that "creates a new operation". Left there, the obvious implementation is a shortcut:
reuse the original approval, since a human already said yes to this workflow with these
arguments. That shortcut converts one approval into two executions and quietly repeals
ADR-003's capability model. Retry is exactly where that pressure will be highest, because
the failure is fresh and the arguments are demonstrably the ones a human approved.

**Audit anchoring.** The hash chain is tamper-*evidence*: an attacker with database write
access can rewrite every row and the chain still verifies (threat T-35, residual risk RR-4).
The fix is to publish chain state somewhere the attacker does not control. "v2 adds external
anchoring" named the intent and nothing else — not the interface, not what an anchor must
guarantee, and not what an operator without specialized infrastructure can actually use.

## Decision

### 1. Governed retry creates a new operation and recalculates everything

`retry_operation(operation_id, ...)` in v2:

1. Creates a **new** operation with `parent_operation_id` set to the original. The original
   is never moved: it has no outgoing edge, and if it is `UNKNOWN` invariant I7 forbids
   acting on it automatically.
2. **Re-runs validation** against the registry snapshot in force *now*, not the snapshot
   the parent used. A workflow whose contract changed since the parent ran does not inherit
   the parent's validity.
3. **Re-runs preflight**, including the live definition-hash check. A parent that ran
   against a definition that has since drifted yields a `BLOCKED` retry.
4. **Recalculates approval from scratch.** The original approval is never reused,
   transferred, or extended. The parent's approval authorized the parent's execution and
   is spent.
5. Mints a fresh handle. The parent's handle stays burned.
6. Writes its own audit records, linked to the parent, so the chain shows a retry as a
   distinct authorized act rather than a repetition of an old one.

**The read-only case, stated precisely because it will look like an exception.** A
`read_only` workflow with `approval: none` retried in v2 goes `PREPARING -> APPROVED` via
T05 without human interaction. That is not approval reuse: T05 is being evaluated afresh
against the current registry snapshot, and it would reach the same conclusion for a first
attempt with the same arguments. The parent's approval record plays no part. If the registry
has since reclassified the workflow — to `external_write`, or to `approval: required` — the
retry takes T04 and waits for a human. **Recalculation, not reuse**, is the distinction, and
it is what keeps ADR-005's guarantee intact under a feature that appears to weaken it.

Invariant **I11** states it: an approval decision authorizes exactly one operation, and no
operation ever inherits, extends, or reuses another operation's approval.

### 2. The `AuditAnchor` interface

v2 defines an interface, not a single integration:

```
AuditAnchor
  publish(anchor: ChainAnchor) -> AnchorReceipt
  verify(anchor: ChainAnchor, receipt: AnchorReceipt) -> AnchorVerification
```

A `ChainAnchor` is the minimum that pins chain state: the sequence number, that entry's
`entry_hash`, the count of entries covered, and the anchoring timestamp. It carries **no
audit content** — no actors, no arguments, no subjects. Anchors are published to systems
Operator does not fully control, so an anchor that leaked operational detail would turn an
integrity control into a disclosure channel.

Requirements on any implementation:

- **Append-only from Operator's side.** Publishing must not be able to overwrite or retract
  a previously published anchor.
- **Independently verifiable.** An auditor holding the anchor store and a database copy can
  confirm agreement without Operator's cooperation.
- **Fail-visible, not fail-open.** An anchor that cannot be published is recorded as a
  publication failure and surfaced; it never silently stops anchoring.
- **Content-free**, as above.

**Two initial implementations, chosen because they cover the two deployments that exist:**

| Implementation | Mechanism | Suits |
|---|---|---|
| **Signed local anchor file** | Append-only file outside the database, each anchor signed with a key held outside the database. Protects against the realistic threat: an attacker who edits the SQLite file but does not hold the signing key. | Single-machine deployments — v1's shape, carried into v2. |
| **Authenticated HTTPS webhook** | Anchors POSTed to an operator-controlled endpoint over TLS with authentication, receipts retained. Puts anchors on a different host under different credentials. | Team deployments that already have a log sink or SIEM. |

Neither defeats an attacker who holds the machine, the signing key, *and* the anchor sink.
Both defeat the database-only tamper that RR-4 actually describes, and that honest scoping
is why they are the starting pair.

**v3 may add** KMS-backed signing (keys in an HSM or cloud KMS), transparency-log
submission (RFC 9162-style, giving third-party-verifiable inclusion proofs), and WORM
storage integration (object-lock buckets, compliance-mode retention). Each is a new
implementation behind the same interface; none changes the chain or the audit schema.

### 3. `list_audit_events` — query scope, pagination, and redaction

Added at v2 stage 00 (contract closure), alongside the tool's contract in
[MCP_TOOLS.md](../MCP_TOOLS.md) section 5.8: querying the chain needed the same rigor
as writing to it, decided here rather than left implicit in a tool schema.

1. **Cursor-based pagination**, identical in shape to v1's `list_operations`: an opaque
   `cursor`, a `limit` bounded 1–100 with a default of 20. No offset-based paging —
   an offset over a monotonically-growing append-only log is exactly the kind of
   pagination that silently skips or duplicates rows under concurrent writes; a cursor
   anchored to `audit_log.seq` does not.
2. **Authorization filters the query, not the result.** Exactly the discipline
   [ADR-019](ADR-019-metrics-cardinality-and-privacy.md) states for `get_metrics`:
   an audit entry whose `subject_id` names a workflow or environment outside the
   caller's authorized scope ([ADR-015](ADR-015-rbac-authorization-evaluation.md)) is
   excluded from the query entirely — never returned with a redacted body, because even
   the existence of an event for an unauthorized workflow is enumeration-adjacent
   information the caller has no standing to receive.
3. **Content redaction is unchanged from v1.** `audit_log.detail` is already redacted
   at write time (BUILD_PLAN section 8.1's own column note: "Redacted"); v2 adds no
   second redaction pass and no un-redaction path for a caller with a broader role —
   there is no role in [ADR-015](ADR-015-rbac-authorization-evaluation.md)'s matrix
   that can see an audit entry's raw, pre-redaction detail. `admin` gets broader
   *query* scope (more workflows and environments visible), never a different view of
   any single entry's content.

## Consequences

### Positive

- Retry cannot become a laundering path for a spent approval. The property is stated as
  invariant I11 and testable directly.
- Recalculation means a retry inherits *current* policy, so a workflow reclassified after a
  failure is governed by the new classification — the safe direction.
- `parent_operation_id`, already in the v1 schema, gets its exact semantics: lineage, never
  authority.
- Anchoring becomes a plug point with stated requirements rather than a promise, so the
  first implementation cannot accidentally define the contract.
- Content-free anchors mean adding an anchor sink does not widen the data-exposure surface —
  which is what makes it plausible to point one at a third-party service.
- The two initial implementations need no infrastructure a v1 operator lacks, so RR-4
  improves for solo operators, not only for enterprises.
- `list_audit_events` reusing v1's cursor shape and inheriting v1's write-time
  redaction means the audit query surface needed no new privacy mechanism of its own —
  it composes two decisions already made elsewhere ([ADR-015](ADR-015-rbac-authorization-evaluation.md),
  BUILD_PLAN section 8.1) rather than adding a third.

### Negative

- Retry in v2 is more expensive than a re-dispatch: full validation, live preflight, and
  possibly a fresh human approval. For a transient downstream failure that is real friction,
  and it is the friction ADR-005 chose deliberately.
- A `read_only` auto-approved retry will still look to some readers like approval reuse. It
  needs the explanation above every time it comes up.
- The signed local anchor file's security rests on key custody, and key custody on a
  single-operator machine is genuinely weak. It raises the bar without clearing it.
- An anchor sink is one more thing that can be down, and fail-visible means it will be
  noticed when it is.

### Neutral

- Anchoring cadence — every N entries, every T seconds, or on demand — is an implementation
  choice per deployment, not part of the interface.
- v2 may resolve an `UNKNOWN` parent by recording a reconciliation *annotation* in the audit
  chain when correlation data exists ([ADR-009](ADR-009-dispatch-correlation.md)). An
  annotation is not a transition; `UNKNOWN` keeps no outgoing edge (invariant I7).

## Alternatives considered

**Let a retry reuse the parent's approval when arguments and definition hash are unchanged.**
The shortcut this ADR exists to foreclose. Rejected: it makes one human decision authorize
an unbounded number of executions whenever the world happens to look unchanged, and
"unchanged" is evaluated by the system, not the human.

**Retry as a transition on the original operation.** Rejected: it needs an edge out of a
terminal state, breaking invariants I2 and I7, and it destroys the one-operation-one-outcome
property that makes the audit trail legible.

**Extend the parent's approval window instead of re-approving.** Rejected: the execution
deadline is short precisely because the world changes; extending it after a failure extends
it exactly when something has demonstrably gone wrong.

**Anchor by writing to a second table in the same database.** Rejected — it is not external.
The attacker in T-35 has write access to that database.

**Blockchain or public transparency log as the v2 default.** Rejected as a default: strong
guarantees, but it requires infrastructure, network egress, and in some cases cost and
public exposure that most operators will not accept. Available in v3 behind the same
interface for those who want it.

**Skip anchoring and rely on filesystem permissions plus backups.** Rejected: backups
detect tampering only if someone compares them, and permissions do not constrain the
operator account that Operator itself runs as.
