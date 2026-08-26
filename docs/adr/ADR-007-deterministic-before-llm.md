# ADR-007: Deterministic enforcement before LLM judgment

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) section 1.4 (P2), [THREAT_MODEL.md](../THREAT_MODEL.md) L-01, L-06

## Context

A product built for LLM clients invites LLM-shaped solutions. Every gate in this system
has a tempting model-based version:

- *Classify how risky this call looks and require approval if it seems dangerous.*
- *Read the workflow's node graph and infer what it does.*
- *Judge whether the caller's stated reason justifies the action.*
- *Summarize the execution failure and decide whether retrying is safe.*

Each would be more flexible than the deterministic alternative, would need no registry
authoring, and would demo beautifully.

Each also shares a disqualifying property: it is **non-deterministic, unauditable, and
manipulable through the same channel it is meant to police**. A classifier reading
caller-supplied text can be argued with by caller-supplied text. Approval fatigue is a
human problem; approval *persuasion* is an LLM problem, and it is worse, because the
persuader and the judge share an input channel.

There is a second, quieter cost. A model-based gate cannot be tested to a property. You
cannot write a Hypothesis test asserting that a classifier never mislabels an
irreversible action, and you cannot show an auditor why a specific call was allowed
eight months ago.

## Decision

**Every security gate is deterministic. LLM judgment is advisory only and never
load-bearing.**

Deterministic, always:

| Gate | Mechanism | Not |
|---|---|---|
| May this workflow run at all? | Registry membership lookup ([ADR-002](ADR-002-default-deny-registry.md)) | Model inference about the workflow |
| Are these arguments valid? | JSON Schema 2020-12, `additionalProperties: false` | Model judgment of reasonableness |
| Is approval required? | `side_effects` and `approval` fields; `risk: high` forces approval (R5, R10) | Model risk classification |
| Is the target unchanged? | `sha256` comparison against `definition_hash` | Model diff summary |
| May this execute now? | State is `APPROVED`, handle unburnt, deadline unelapsed, fingerprint matches ([ADR-003](ADR-003-operation-handles.md)) | Model judgment of the moment |
| Should this be retried? | Never automatically ([ADR-005](ADR-005-no-automatic-retry-v1.md)) | Model assessment of safety |
| What may leave the process? | JSONPath redaction plus an allowlist projection | Model judgment of sensitivity |

Advisory, never load-bearing:

- `reason` on `prepare_operation` — shown to the human approver and recorded in the
  audit log. **No gate reads it.** A perfect justification changes nothing (L-06).
- `risk` and `description` — surfaced to help the model choose well and the human decide
  well. They inform; `side_effects` and `approval` decide.
- v3's remediation assistant — proposes; never applies.

The test: **if a model output disappeared or was replaced with an adversary's text,
would any security property change?** If yes, that gate is misdesigned.

## Consequences

### Positive

- Every gate is testable as a property. This is what makes BUILD_PLAN section 10.2
  possible at all — you can generate inputs against a schema check, not against a
  classifier.
- The security argument is auditable. "Why was this allowed?" answers with a registry
  entry, a schema, a hash, and an approval record — reproducible eight months later.
- Prompt injection cannot argue its way past a gate. It can produce a *request*; it
  cannot produce an approval, a registry entry, or a matching hash (L-01).
- Behavior is reproducible. The same inputs yield the same decision, always.
- No inference cost, no latency, and no dependency on a model provider inside the
  enforcement path — which also means Operator's security does not degrade when a model
  is swapped or upgraded.
- The failure mode is closed. A deterministic check that cannot evaluate fails to
  `BLOCKED`; a classifier that is uncertain returns something plausible.

### Negative

- More operator work up front. Someone must classify each workflow and author each
  schema, where a classifier would have guessed (ADR-002's main cost).
- Less flexible. A gate cannot use context a schema cannot express — "this argument is
  fine on weekdays but not during a freeze."
- Coarser. `side_effects: irreversible` treats a $1 refund and a $1M wire identically.
  Expressing that difference means an operator writing it into the schema or splitting
  the workflow.
- We forgo genuinely useful assistance, such as an LLM summarizing a definition diff for
  a human reviewer. Note that this specific case is *allowed* — it assists a human
  decision rather than replacing a gate — and it lands in v2 alongside
  `diff_workflow_definition`.

### Neutral

- This constrains Operator's own internals, not its clients. The calling model may reason
  however it likes about *what to ask for*; the constraint is that its reasoning never
  becomes the thing that authorizes.
- v3's evaluation lab uses models to *generate* test cases and *analyze* results, which
  is fine — the promotion gate remains a deterministic pass/fail against fixtures.

## Alternatives considered

**LLM risk classifier deciding when approval is required.** Rejected: the classifier
reads caller-influenced text, so the caller can influence whether it is gated. It is
also untestable as a property and unexplainable to an auditor.

**LLM reads the node graph and infers `side_effects`.** Tempting, because it removes the
most tedious registry field. Rejected: a false `read_only` silently disables the human
gate for a workflow that writes — the exact failure this product exists to prevent — and
node semantics depend on runtime parameters a static read cannot resolve.

**Hybrid: deterministic gates, with an LLM able to *escalate* but never *de-escalate*.**
The only defensible hybrid, and deferred rather than rejected. It is strictly additive
safety. Not in v1: it adds a model dependency in the request path for a benefit
(occasionally demanding an approval the registry did not require) that a correctly
authored registry already provides.

**LLM-generated redaction paths.** Rejected as a gate; useful as an authoring aid. v2
may *suggest* redaction paths for an operator to review and commit — the operator's
commit is what makes them real.
