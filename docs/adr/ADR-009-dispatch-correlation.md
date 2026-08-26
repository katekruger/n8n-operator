# ADR-009: Dispatch correlation and indeterminate outcomes

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Lead architect
- **Phase:** 0.1 (architecture-decision closure), implemented in phases 4 and 7
- **Related:** [ADR-005](ADR-005-no-automatic-retry-v1.md), [ADR-006](ADR-006-server-owned-n8n-credentials.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 5.2, 6.3, [THREAT_MODEL.md](../THREAT_MODEL.md) T-40, T-41

## Context

Two unresolved questions from phase 0 are really one question — *what does Operator
actually know about a dispatch it cannot confirm?* — plus a nearby one about credentials
that fails in the same way if answered carelessly.

**Correlation.** n8n webhook triggers respond with whatever the workflow's response node
produces. There is no protocol-level guarantee of an execution identifier, so in the
general case Operator dispatches a request and, on timeout, holds nothing that lets it ask
n8n "did execution X run?". `execution_results.n8n_execution_id` exists in the schema and
phase 0 had no committed way to populate it.

**The temptation.** A timeout on a request that produced no visible effect *feels* like a
non-event, and it would be convenient to record it as one. It is not: a read timeout says
only that no response arrived within the window. The workflow may be running, may have
completed after the deadline, and may already have sent the email.

**Credentials.** Preflight's `MISSING_NODE_CREDENTIALS` check has the same shape of
hazard. n8n can report whether a node has a credential *bound*. Whether that credential is
*valid* — unexpired, unrevoked, still authorized downstream — is a different claim, and
reporting the first as though it were the second manufactures false assurance at exactly
the moment an operator is deciding whether to approve.

## Decision

**Indeterminacy is preserved, never resolved by inference. Correlation is an opt-in
capability a workflow may provide, and its absence degrades reconciliation — never safety.**

### 1. Indeterminate outcomes

1. A timeout, connection loss, or unparseable response *after dispatch* transitions
   `EXECUTING -> UNKNOWN` (T15). `UNKNOWN` remains terminal.
2. **Operator never infers that a timed-out dispatch did not run.** There is no code path,
   heuristic, error-class check, or elapsed-time rule that converts a timeout into
   `FAILED`, and none that converts it into `SUCCEEDED`.
3. `execute_operation` returns `DISPATCH_INDETERMINATE` with `retryable: false` and a
   message that tells the caller plainly to verify downstream rather than retry.
4. **AC-16 is not weakened.** Everything here adds information *around* `UNKNOWN`; nothing
   adds an exit from it.

### 2. The Operator response envelope

Where a workflow can be authored to return one, Operator accepts a response envelope
carrying the n8n execution identifier:

```json
{ "n8n_operator": { "execution_id": "1042" }, "data": { "…": "the workflow's own result" } }
```

- A registry entry declares support with `trigger.correlation: response_envelope`; the
  default is `none` (BUILD_PLAN section 6.3).
- When declared and present, `execution_id` is recorded on `execution_results` and
  `operations`, enabling reconciliation and richer `get_execution_log` output.
- When declared but absent or malformed, the dispatch is **not** failed for that reason
  alone: the outcome is classified on its own merits and the missing correlation is
  recorded as a finding. A workflow that returns a result is not broken because its
  envelope is.
- The envelope is unwrapped before redaction and shaping. `execution_id` is an n8n-internal
  identifier used for server-side reconciliation and debugging output; it is not an n8n
  *workflow* ID and does not breach boundary B5.

### 3. Workflows without correlation

They remain fully executable. Registering one is not an error and not a warning at load
time. What they lose is stated honestly rather than papered over:

- reconciliation after `UNKNOWN` is manual — an operator checks the downstream system;
- `get_execution_log` may be limited to what the dispatch itself observed;
- v2's governed retry has less to work with when deciding whether a re-run is safe.

**Preflight reports the limitation.** A `correlation` check returns `warn` with code
`NO_EXECUTION_CORRELATION` when the registry entry declares `correlation: none`. A `warn`
never blocks: `ready` stays `true` and no operation is `BLOCKED` for it. It exists so that
the reduced capability is visible before an operator approves, not discovered during an
incident.

### 4. Credential inspection

1. Preflight **may** detect that a node lacks a credential binding, reported as
   `MISSING_NODE_CREDENTIALS`.
2. It **must** distinguish binding presence from credential validity. The check is named
   and worded for what it does: bindings are present, not credentials work.
3. Operator **never** asserts that a credential is valid without a supported n8n mechanism
   that actually tests it. Absent such a mechanism the check reports status
   `unverifiable` with code `CREDENTIAL_VALIDITY_UNVERIFIED` — never `pass`.
4. `unverifiable`, like `warn`, is informational and does not block.

This introduces two non-blocking check statuses alongside `pass`, `fail`, and `skipped`:
`warn` and `unverifiable` (MCP_TOOLS.md section 2.5). Only `fail` produces `BLOCKED`.

### 5. Unattended execution stays available, and says so

T05 — `PREPARING -> APPROVED` with no human in the loop — is **preserved**. It requires
**both** `side_effects: read_only` and `approval: none`; neither alone suffices, and the
registry default remains `approval: required`, so unattended running is opted into
deliberately, per workflow.

Preflight emits an `UNATTENDED_EXECUTION` `warn` for every workflow eligible for T05. It is
not a complaint about the entry — it names the actual trust relationship. Operator cannot
read a node graph and confirm that a workflow only reads; it is trusting the **operator's**
`side_effects` classification, and that classification is the only thing between an agent
and an unsupervised run (threat T-28, residual risk RR-2). A gate that rests on
operator-supplied metadata should say so where the human can see it, which is at approval
time for every *other* workflow — and, for this one, at preflight, because there is no
approval step in which to say it.

### 6. Preparation stays coupled to live preflight

Phase 0 left open whether `prepare_operation` should require a successful live preflight,
since coupling them means an n8n outage prevents even queueing work.

**It stays coupled.** Preflight failure — including `INSTANCE_UNREACHABLE` — yields
`BLOCKED` (T03), and **v1 does not queue unverified work.** An operation that has not been
verified against the live instance is never offered for approval.

The reasoning is that the alternative quietly relocates the drift check. An operation
prepared while n8n was unreachable would have to be verified later — at approval time, or at
execution time — which means either asking a human to approve something whose target was
never confirmed, or converting an approved operation into a `BLOCKED` one after the human
has already decided. Both are worse than telling the caller now that the instance is down.
An outage meaning "no new work is authorized" is the safe failure direction, and it matches
the availability stance in THREAT_MODEL section 8.

## Consequences

### Positive

- The system never lies about what it knows. Every claim preflight makes is one it can
  actually support, and `UNKNOWN` continues to mean unknown.
- Correlation becomes available where it matters most — a workflow whose duplicate
  execution would be expensive is exactly the one worth authoring an envelope for — without
  making it a precondition for using Operator at all.
- The reduced-capability case is surfaced *before* approval, so an operator can choose to
  add an envelope to a high-stakes workflow rather than learning the gap exists while
  reconciling one.
- `execution_results.n8n_execution_id` gets a defined provenance rather than remaining an
  aspirational column.
- The credential wording removes a false-assurance failure that would have been very easy
  to ship and very hard to notice.
- `UNATTENDED_EXECUTION` puts the one trust assumption Operator cannot verify in front of
  the operator, in the one lifecycle where no approval page will ever show it to them.
- Coupling preparation to live preflight keeps drift detection in exactly one place. There
  is no second, later verification path to get subtly wrong.

### Negative

- Correlation requires workflow authoring: a response node shaped to the envelope. Operator
  cannot supply it, only consume it.
- Two capability tiers now exist, and `get_execution_log` output differs between them —
  more documentation, and a support question we will answer repeatedly.
- `warn` and `unverifiable` enlarge the preflight vocabulary, and non-blocking findings are
  easy for a model to skim past. They are surfaced to the human at approval for that reason.
- `MISSING_NODE_CREDENTIALS` is now explicitly a weaker signal than its name suggests to a
  casual reader, which is honest but disappointing.
- An n8n outage blocks preparation entirely: a caller cannot even stage work to run later.
  For a long outage that is real lost throughput, deliberately traded for never approving
  an unverified target.
- `UNATTENDED_EXECUTION` fires on every preflight for an auto-approved workflow, so it is a
  warning operators will see constantly and may learn to ignore — the familiar hazard of a
  warning that is always on.

### Neutral

- The envelope is deliberately additive and namespaced (`n8n_operator`), so a workflow can
  adopt it without changing what its own consumers see under `data`.
- v2 may add reconciliation that polls n8n for a known `execution_id` to resolve an
  `UNKNOWN` **into an audit annotation** — never into a state transition, since `UNKNOWN`
  has no outgoing edge (invariant I7).
- If a future n8n version guarantees an execution identifier on webhook responses,
  `correlation` gains a value and the default may change. The safety story does not.

## Alternatives considered

**Infer non-execution from a connect-timeout.** Distinguishing "connection refused before
send" from "sent, response lost" looks tractable and is the same trap ADR-005 declined at
the retry layer. Rejected for the same reason: it makes a safety property depend on the
exact fidelity of an HTTP client's exception taxonomy, forever, and the payoff is
converting a small number of `UNKNOWN`s into `FAILED`.

**Require correlation for every registered workflow.** Would make reconciliation universal.
Rejected: it makes Operator unusable against existing n8n instances, and the pressure to
adopt it would be answered by authoring an envelope that lies.

**Poll n8n's executions API after a timeout to find a matching run.** Attractive, and
rejected for v1: without a correlation ID the match is heuristic — by workflow, by time
window — and a wrong match resolves an `UNKNOWN` to a confident, false `SUCCEEDED`. Worse
than the honest unknown. Revisit in v2 *only* for workflows that supply an execution ID,
where the lookup is exact.

**Test credentials during preflight by making a downstream call.** Would give a real
validity signal, and rejected: preflight would itself cause the side effect it exists to
gate. A read-only credential probe is not generally available, and inventing one per node
type is unbounded work.

**Allow preparation while n8n is unreachable, deferring preflight to approval or execute.**
Would let work be staged through an outage. Rejected: it either asks a human to approve an
operation whose target was never verified, or invalidates an approval after the fact — and
it splits drift detection across two code paths, which is how the check that matters most
acquires a gap.

**Drop T05 and require human approval for everything.** Simpler to reason about, and
rejected: it would make read-only reporting workflows — the safe, high-frequency case Operator
should be best at — require a click each time, which trains operators to approve reflexively
and degrades the gate where it actually matters.
