# ADR-005: No automatic retries in v1

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) sections 5.1, 5.2 (T15), [THREAT_MODEL.md](../THREAT_MODEL.md) L-04

## Context

Retries are reflexive in HTTP client code, and every library offers them. They are also
the single most dangerous default in a system whose purpose is to make side effects
deliberate.

The problem is that a failed request has three outcomes, and only one is safely
retryable:

1. **Definitely did not happen** — connection refused before the request was sent.
2. **Definitely happened, response lost** — the workflow ran; the reply did not arrive.
3. **Unknown** — a timeout. The request may be executing right now.

From the client side, (2) and (3) are frequently indistinguishable, and n8n webhooks
give us nothing that reliably separates them. A retry in case (2) or (3) duplicates the
side effect. For `irreversible` workflows — an email sent, a payment made — a duplicate
is not an inconvenience; it is the failure the product exists to prevent.

Two more factors sharpen this. First, an approval authorizes *one* invocation; a retry
performs a second invocation under a single approval, which quietly violates the
capability model ([ADR-003](ADR-003-operation-handles.md)). Second, the caller is an LLM,
and repetition under uncertainty is its characteristic failure mode. A system that
retries on the model's behalf compounds a tendency it should be dampening.

## Decision

**v1 never retries anything automatically. An indeterminate dispatch is a terminal state
requiring human resolution.**

1. The n8n client (`n8n/client.py`) has no retry logic, no backoff helper, and no
   `retries=` transport setting. A contract test greps for their absence (AC-17).
2. A dispatch that times out, loses its connection, or returns an ambiguous response
   transitions `EXECUTING -> UNKNOWN` (T15).
3. `UNKNOWN` is terminal. No transition leads out of it (invariants I2, I7).
4. `execute_operation` returns `DISPATCH_INDETERMINATE` with `retryable: false` and a
   message written to tell the model, in plain words, not to retry and to verify the
   downstream system instead (MCP_TOOLS.md section 2.8).
5. Handles are single-use, so a client that retries anyway gets `HANDLE_ALREADY_USED`
   and dispatches nothing (I4). The architecture makes the wrong behavior impossible,
   rather than merely discouraged.
6. A genuine re-attempt means preparing a **new** operation — new validation, new
   preflight, new human approval.

**v2 adds governed retries** as an explicit, separately-audited `retry_operation` that
creates a new operation linked by `parent_operation_id`. It never revives the original.
Even then it is a caller-initiated, policy-checked, approval-gated act — not a transport
behavior.

## Consequences

### Positive

- No duplicate side effect can originate from Operator. The strongest guarantee the
  product makes, and the one most worth having.
- Ambiguity stays visible. `UNKNOWN` is a state an operator can query, count, and
  reconcile — rather than a retry that silently converted an unknown into a duplicate.
- The capability model stays honest: one approval, one invocation, always.
- It dampens the LLM retry reflex instead of amplifying it (L-04).
- Failure handling is dramatically simpler: no backoff, no jitter, no retry budgets, no
  idempotency-key negotiation with n8n, no partial-retry semantics.

### Negative

- Transient network blips become manual work. A five-second n8n hiccup that a single
  retry would have papered over instead produces an `UNKNOWN` a human must resolve.
- Operators must actually reconcile `UNKNOWN` operations by checking the downstream
  system. There is no automation for this in v1 (residual risk RR-5).
- An over-tight `timeout_seconds` converts slow successes into manual work, which puts
  real weight on choosing that limit well (WORKFLOW_REGISTRY.md section 8).
- It will look like a missing feature to anyone who has not thought about case (2).

### Neutral

- Read-only workflows would be safe to retry, and we still do not, in v1. A blanket rule
  is verifiable by grep; a conditional rule depends on `side_effects` being correct in
  every registry entry, and that is operator-supplied data. v2's governed retry can be
  conditioned on it, under RBAC, with an audit record.
- `get_instance_health` and preflight are pure reads with no side effects; a caller may
  call them again freely. This decision governs *dispatch*, not every request.

## Alternatives considered

**Retry only `read_only` workflows.** Rejected for v1 as above: it makes a security
property depend on operator-authored metadata and turns a greppable invariant into a
conditional one. Revisit in v2.

**Retry only on connection-refused (case 1).** Genuinely safe, and rejected anyway: it
requires the httpx exception taxonomy to map perfectly onto "the request was never sent"
in every case, forever. That is a subtle, silent, load-bearing assumption. The value
recovered — one saved retry on a rare error — does not justify it.

**Idempotency keys passed through to n8n.** The right long-term answer, and unavailable:
it requires every registered workflow to implement idempotent handling, which Operator
cannot verify or enforce. Possible in v3, where the compiler could generate the handling
and the evaluation lab could test it.

**Automatic reconciliation of `UNKNOWN` by polling n8n executions.** Attractive, deferred
to v2. It requires correlating a dispatch with an execution record — a correlation
webhook triggers do not reliably provide — and a wrong correlation would resolve an
`UNKNOWN` to a false `SUCCEEDED`, which is worse than leaving it unknown.
