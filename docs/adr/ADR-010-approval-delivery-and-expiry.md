# ADR-010: Approval delivery and expiry semantics

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Lead architect
- **Phase:** 0.1 (architecture-decision closure), implemented in phases 6 and 8
- **Related:** [ADR-003](ADR-003-operation-handles.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 5.2, 9.2, [THREAT_MODEL.md](../THREAT_MODEL.md) T-38

## Context

Phase 0 made approval out-of-band and put it in a loopback browser page. Two gaps followed
from that, both flagged as unresolved.

**Delivery.** `prepare_operation` returns `approval_url` — a `127.0.0.1` address. That
address is meaningful only to a caller sitting on the same machine. For a remote MCP client
over Streamable HTTP it is worse than useless: it is a plausible-looking URL that resolves,
for the model and often for the human reading its output, to *something on their own
machine that isn't there*. The likely outcomes are a confusing error, a model reporting
that it "sent the approval link", or an operation that quietly expires while everyone
believes a link was delivered. A control surface that mispresents itself is a defect even
when it is technically loopback-safe.

**Expiry.** T08 and T11 were attributed to "Clock", with a sweeper inside the approval app
and a lazy read-time check as a defensive fallback. That leaves the authority ambiguous —
two mechanisms, neither designated — and it leaves stdio-only deployments, where no
approval app runs, with no defined expiry behavior at all. Ambiguity in *which* mechanism
is authoritative is exactly how an expired approval gets executed once.

## Decision

### 1. The CLI is the canonical approval channel in v1

`n8n-operator` on the operator's machine — reading a pending operation and approving or
rejecting it — is the approval mechanism v1 commits to. It works in every deployment,
needs no browser, no listener, and no reachability assumptions.

**The localhost approval page is convenience only.** It remains supported and remains the
nicer experience when the operator is at the machine, but it is no longer the mechanism the
product's guarantees rest on. Nothing may require it: every approval and rejection is
reachable through the CLI, and phase 6's tests exercise the CLI path as primary.

Both channels write the same transitions (T06, T07) through the same core use case, so
neither is a second implementation of policy ([ADR-001](ADR-001-portable-mcp-core.md)).
Both remain outside the MCP channel, so boundary B4 is untouched: there is still no tool
that approves.

### 2. A remote caller is never handed an unreachable URL

`prepare_operation` returns, for an operation awaiting approval:

- `approval_required: true` — unambiguous, machine-readable;
- `operation_id` — what the human needs to act on it;
- `approval_instructions` — human-readable text naming the CLI command to run;
- `approval_url` — **only when the caller is local.**

Locality is decided by the transport, deterministically, not guessed:

| Transport | Treated as | `approval_url` |
|---|---|---|
| stdio | local — the host launched the process on this machine | included |
| Streamable HTTP bound to loopback | local | included |
| Streamable HTTP bound to a non-loopback interface | remote | **omitted** |

`N8N_OPERATOR_APPROVAL_URL_EXPOSURE` may be set to `never` to suppress the URL everywhere;
it can never force exposure to a remote caller. Invariant **I12** states the property: an
approval URL is never returned to a caller that cannot reach it.

Omission is silent in the sense that it is not an error, and explicit in the sense that
`approval_required` and `approval_instructions` are always present. The model is told what
is needed and who must do it; it is not handed a link that lies.

### 3. Lazy transactional expiry is authoritative

**Every read of, and every action on, an operation first applies any overdue T08 or T11
transition, in the same transaction, before evaluating state.** That includes
`get_operation`, `list_operations`, `execute_operation`, `cancel_operation`, both approval
channels, and every CLI equivalent.

Consequences of designating it authoritative:

- Correctness never depends on a process being up. There is no deployment in which an
  expired approval can be acted on, because the act itself expires it first.
- The transition is a real transition: `operation_events` row, `audit_log` row, same
  transaction (invariant I6). Lazy expiry is not a display convention.
- It is idempotent and race-safe under the `state_version` guard — concurrent readers
  cannot double-apply it.

Invariant **I9** states it: no operation is ever read or acted upon in a state whose
deadline has already passed.

### 4. The sweeper is best-effort; maintenance is explicit

- The approval app **may** run a periodic sweeper. It is an optimization — it makes the
  `EXPIRED` audit record appear near the wall-clock moment of expiry rather than at next
  touch. Nothing depends on it.
- `n8n-operator operations expire` applies all overdue transitions on demand, for
  deployments that want expiry recorded on a schedule (cron, a systemd timer) without
  running the approval app.

**The stdio-only consequence, stated plainly:** in a deployment with no approval app and no
scheduled maintenance command, an operation that expires is still *treated* as expired the
instant anything touches it, but its `EXPIRED` audit event carries the timestamp of that
touch rather than of the deadline. An operation nobody ever looks at again may never get an
`EXPIRED` row. This is a fidelity limitation of the audit *timeline*, not a safety
limitation: no expired operation is ever executed. It is documented in BUILD_PLAN section
9.5 and ARCHITECTURE section 8 so nobody discovers it from a gap in an audit export.

## Consequences

### Positive

- Approval works in every v1 deployment topology, including headless servers and remote
  Streamable HTTP, which the phase-0 design did not actually cover.
- The remote-client failure mode is designed out rather than documented as a caveat.
- One designated authority for expiry replaces two undesignated mechanisms. The question
  "which one was responsible?" cannot arise during an incident.
- Safety no longer depends on process liveness, which is the right dependency to remove
  from a control that gates side effects.
- `approval_required` gives models a boolean to branch on instead of inferring intent from
  the presence of a URL — a better contract for the actual consumer.

### Negative

- Approving via CLI is worse ergonomics than clicking a button, and the CLI must now render
  arguments, risk, side-effect class, and drift status well enough to support a real
  decision. That is meaningful phase-6 work on a surface that was going to be thin.
- Two approval channels means two paths to test and keep behaviourally identical.
- Lazy expiry adds a write to operations that were nominally reads, so `get_operation` can
  no longer be served from a read-only transaction.
- The audit timeline can under-record `EXPIRED` in stdio-only deployments. Real, and the
  honest trade against requiring a resident process.

### Neutral

- Remote approval delivery — email, Slack, a push notification — is a v2 concern
  (`request_approval` routing). v1's answer is that the operator is a person with shell
  access to the machine Operator runs on, which is true of every v1 deployment by
  construction.
- The approval page keeps its full v1 security posture: loopback bind, CSRF, `Origin` and
  `Host` validation, single-use hashed tokens (boundary B10). Downgrading it to
  "convenience" changes what depends on it, not how it is defended.

## Alternatives considered

**Keep the page canonical and return the URL to everyone.** The phase-0 position, now
rejected: it is precisely the mispresentation described above, and it leaves headless
deployments with no approval path.

**Return a URL built from a configured externally-reachable base.** Tempting, and rejected
for v1: it means exposing the approval app beyond loopback, which is the one thing boundary
B10 forbids. Doing it safely requires authentication and TLS on the approval surface —
a v2 feature with a v2's worth of design, not a config field.

**Make the sweeper authoritative and require the approval app.** Rejected: it makes a
safety property depend on a process being up, and turns "the approval app crashed" into
"expired approvals became executable".

**Expire only in a maintenance command.** Rejected outright: between runs, expired
approvals would be executable. Expiry must be enforced at the point of use.

**Report expiry lazily but write the audit row asynchronously.** Rejected: it breaks
invariant I6's single-transaction rule for the sake of read performance nobody needs at v1
scale.
