# ADR-003: Operation handles as single-use capabilities

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) section 5, [THREAT_MODEL.md](../THREAT_MODEL.md) T-04, T-05, T-06, L-05

## Context

A single `run_workflow(workflow_id, arguments)` tool is the natural MCP shape, and it is
wrong for anything with side effects. It conflates four separable decisions — *may this
run at all*, *are these arguments valid*, *is the target still what we approved*, and
*is now the moment* — into one call that either fires or does not.

That conflation costs specific things:

- There is no point at which a human can review a concrete, pending action.
- A retried tool call is indistinguishable from a second intentional call, so exactly-once
  becomes impossible.
- Approval, if bolted on, would be approval of a *workflow*, not of *this invocation with
  these arguments* — and the difference is the entire attack surface (T-05).
- An LLM's failure mode is repetition. A single fire-and-forget tool invites it.

## Decision

**Execution requires an operation handle: an opaque, server-minted, single-use capability
bound to a specific principal, workflow, argument fingerprint, and approval.**

1. `prepare_operation` creates an operation record and returns `operation_id` =
   `op_<ULID>`, which is the handle.
2. The handle is bound at mint time to `(principal_id, workflow_id, definition_hash,
   argument_fingerprint)`. `argument_fingerprint` is `sha256` over canonical JSON of the
   arguments as submitted.
3. Possessing a handle is not authority. Authority is the operation reaching `APPROVED`,
   which for anything above `read_only` requires a human acting out-of-band.
4. `execute_operation(operation_id, handle)` verifies the binding, the state, the
   deadline, and the live definition hash, then **burns** the handle with a conditional
   update (`WHERE handle_burned_at IS NULL`) whose affected-row count is checked. Zero
   rows means `HANDLE_ALREADY_USED` and nothing is dispatched.
5. A burnt handle is never re-mintable. A v2 governed retry creates a *new* operation
   linked by `parent_operation_id` ([ADR-005](ADR-005-no-automatic-retry-v1.md)).

## Consequences

### Positive

- **Exactly-once is enforced by the database**, not by application logic hoping to win a
  race. Compare-and-set on the burn is the whole mechanism (invariant I4, AC-10).
- **Approval is of a concrete action.** The human sees the exact arguments that the
  fingerprint will hold execution to. Approving cannot be widened afterwards (T-05, I5).
- **The TOCTOU window is closed at both ends.** Drift is checked at preflight *and* again
  at execute, so a workflow changed after approval cannot run under it (T-25, AC-13).
- **Idempotency composes cleanly.** A client key maps to an operation; replaying prepare
  returns the same operation rather than minting a second capability (I8, AC-11).
- **The state machine has something to be about.** Twelve states describing the life of a
  named, durable object is coherent; twelve states describing a function call is not.
- **Audit becomes narrative.** Every record hangs off an operation ID, so "what happened
  and on whose authority" is one query.

### Negative

- Three round trips (`prepare`, poll, `execute`) where one would do, and models must be
  taught the shape — mitigated by making every result state self-describing and every
  error say what to do next.
- Operations accumulate rows even when nothing executes. Acceptable: they are the audit
  trail of what was *attempted*, which is exactly what an auditor wants.
- Handle expiry adds real states (`EXPIRED`) and real failure modes. Worth it: an
  approval that stays executable indefinitely is a latent capability sitting in a log.

### Neutral

- The handle *is* the operation ID rather than a separate secret. Simpler, and safe
  because the ID alone confers nothing — the operation must also be `APPROVED`, unburnt,
  in-deadline, and drift-free. A separate bearer secret would add a second thing to leak
  without adding a check.
- ULIDs over UUIDv4: lexicographically sortable, so they order chronologically for free
  in listings and in the append-only event log.

## Alternatives considered

**`run_workflow` with a `confirm: true` argument.** The confirmation is inside the
untrusted channel; a manipulated model simply sets the flag. Rejected — it is a comment,
not a control.

**Signed capability tokens (JWT/macaroon) instead of database rows.** Stateless and
verifiable offline, but single-use requires a server-side burn list anyway, so the
statelessness is illusory. Rejected: a database row is simpler and is already required
for audit and state.

**Time-boxed session approval** ("approve this workflow for the next hour"). Rejected for
v1: it re-opens exactly the gap handles close — approval of a workflow rather than of an
invocation — and any argument may be substituted within the window. May return in v2 as
an explicit, RBAC-gated, audited policy for `read_only` workflows only.

**Optimistic execution with compensation.** Run, then undo on rejection. Rejected:
`irreversible` side effects have no compensating action, which is what makes them
irreversible.
