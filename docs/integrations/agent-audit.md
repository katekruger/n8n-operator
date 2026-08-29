# agent-audit integration

n8n-operator already has the approval semantics
[agent-audit](https://github.com/katekruger/agent-audit) exists to make
portable: a twelve-state operation lifecycle (`docs/BUILD_PLAN.md`
sections 5.1–5.2), a hash-chained `audit_log` (`audit/writer.py`,
invariant I6). What it doesn't have on its own is a record another
team's observability backend can ingest without an n8n-operator-specific
adapter — `audit_log` is our schema, not a portable one.

`src/n8n_operator/integrations/agent_audit.py` is a thin, best-effort
bridge from our own transition vocabulary onto agent-audit's three-phase
event model (`proposed` / `decided` / `executed`), emitted as
OpenTelemetry LogRecord Events.

## Status: optional, provisional

- **Fully optional.** If `agent_audit_record` is not installed, every
  function in this module is a no-op. Nothing about n8n-operator's own
  audit log, approval flow, or test suite depends on it, and the default
  `uv sync` does not install it.
- **Provisional dependency source.** agent-audit is not yet on PyPI, and
  its GitHub repository is currently private. The `agent-audit`
  dependency group in `pyproject.toml` points at a specific branch of
  that repo via a direct git reference — it will not resolve for anyone
  without access, and must be repointed at a normal PyPI version once
  agent-audit publishes. See the comment above that dependency group for
  the exact branch currently pinned.
- **Never in the transaction.** Nothing in this module participates in
  `core/service.py`'s `session_scope`. A slow or failing agent-audit
  exporter must never affect whether an operation transition commits —
  `agent_audit_record.Emitter` itself guarantees this (it never raises
  for a telemetry failure), and every call site here additionally wraps
  its own logic in a broad `except Exception` as defense in depth.

## Install

```bash
uv sync --group agent-audit
```

## Transition → event mapping

| Transition | n8n-operator action | agent-audit call | Reasoning |
|---|---|---|---|
| T01 (creation) | `operation.prepared` | `proposed` | The operation row's creation is the moment an action is determined and about to seek authorization. |
| T02 (→ INVALID) | `operation.invalid` | `decided(auto_deny, policy)`, `cost.wasted=true` | Malformed input, rejected before any approval was sought — an automatic denial. |
| T03 (→ BLOCKED) | `operation.blocked` | `decided(deny, policy)`, `cost.wasted=true` | Blocked by policy before approval — a judgment call between `deny` and `auto_deny`; either forbids execution identically (see below). |
| T04 (→ PENDING_APPROVAL) | `operation.pending_approval` | *(none)* | Intermediate state — no decision has been reached yet. |
| T05 (→ APPROVED, auto) | `operation.auto_approved` | `decided(auto_allow, policy)` | An automatic approval, recorded as a decision so its provenance (which policy, how fast) is captured — spec Pattern A permits omitting this Record entirely, but n8n-operator's own audit log always records it, so we do too. |
| T06 (PENDING → APPROVED) | `operation.approved` | `decided(allow, human)` | A human approved. |
| T07 (PENDING → REJECTED) | `operation.rejected` | `decided(deny, human)`, `cost.wasted=true` | A human rejected. |
| T08 (PENDING → EXPIRED) | `operation.expired` | `decided(timeout, timeout)`, `cost.wasted=true` | The approval request itself timed out — no principal ever responded. |
| T09 (PENDING → CANCELED) | `operation.canceled` | `decided(cancel, human)`, `cost.wasted=true` | Canceled before any decision was reached. |
| T10 (APPROVED → EXECUTING) | `operation.execution_started` | *(none)* | Intermediate state — the decision already landed at T05/T06; execution hasn't resolved yet. |
| T11 (APPROVED → EXPIRED) | `operation.expired` | `executed(not_executed, expired)` | Approved, but the execution deadline passed before it ran. The T05/T06 decision stands unchanged — see below. |
| T12 (APPROVED → CANCELED) | `operation.canceled` | `executed(not_executed, cancelled)` | Approved, then canceled before running. Same principle as T11. |
| T13 (→ SUCCEEDED) | `operation.succeeded` | `executed(success)` | |
| T14 (→ FAILED) | `operation.failed` | `executed(failure)` | |
| T15 (→ UNKNOWN) | `operation.indeterminate` | `executed(failure)` | agent-audit's `Outcome` enum has no "indeterminate" value; `failure` is the closer of the two options over `success`. Flagged as a known simplification, not a perfect fit. |
| *(prepare denied — no operation row)* | `operation.prepare_denied` | `proposed` + `decided(deny, policy)`, `cost.wasted=true` | ADR-011's "the attempt is still audited" applies here too — oversized arguments or a rate limit refusal, mints its own synthetic `action_id` since no operation exists to correlate against. |

An operation transitions through at most one of T05 or T06 (never both),
so `decided` is emitted exactly once per operation — consistent with
agent-audit's `agent_audit.action.id` correlation model
(`spec/SPECIFICATION.md` §5.3).

### A finding from dogfooding: `auto_deny` needed a spec fix

The first version of this integration used `agent_audit.decision =
auto_deny` for T02/T03 and hit a real bug in agent-audit itself: its
schema required `cost.wasted = true` only for `deny`, `cancel`, and
`timeout` — `auto_deny` was left out, even though an automatic denial
forbids execution exactly as a human one does. That's now fixed upstream
(see [katekruger/agent-audit#5](https://github.com/katekruger/agent-audit/pull/5)),
and this integration is what surfaced it — the kind of thing "if the
abstraction doesn't survive contact with a real server, fix the
abstraction" is for.

## Why T03 uses `deny` rather than `auto_deny`

A judgment call, noted here rather than left silent: T02 (malformed
input) is unambiguously automatic — nothing resembling a policy decision
happened, so `auto_deny` fits cleanly. T03 (blocked by policy) is closer
to a genuine policy *decision*, even though no human was in the loop, so
either `deny` or `auto_deny` would be defensible. This integration uses
`deny` for T03 to keep the historical audit trail's causal story
("blocked" reads as a decision) distinct from T02's "never entered
consideration" — but this is exactly the difference `auto_deny` exists to
capture, not to erase, and it will not affect the *forbids-execution*
behavior either way.

## Decision unchanged by a later execution result

Per `spec/SPECIFICATION.md` §6.6: a decision recorded on a `decided`
Record is never revised by a later `executed` Record. T11 and T12 both
follow an *already-decided* (T05 or T06) operation — the `allow` decision
from that earlier transition stands in the agent-audit record exactly as
it was reached, alongside the `not_executed` outcome, not replaced by it.
This mirrors n8n-operator's own audit log, which never edits or removes
a prior transition's row either.
