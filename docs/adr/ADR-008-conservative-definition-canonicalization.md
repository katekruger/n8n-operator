# ADR-008: Conservative workflow-definition canonicalization

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Lead architect
- **Phase:** 0.1 (architecture-decision closure), implemented in phase 4
- **Related:** [ADR-002](ADR-002-default-deny-registry.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 6.8, [THREAT_MODEL.md](../THREAT_MODEL.md) T-24, T-25, T-39

## Context

`definition_hash` is the mechanism behind the product's strongest claim: *the graph you
reviewed is the graph that runs, or nothing runs.* It is checked at preflight and again
at execute (boundary B8), and it is what makes an approval an approval of something
specific rather than of a name.

The hash is taken over a **canonicalized** form of the n8n workflow definition, because
the raw definition carries fields that change without the workflow changing: `updatedAt`,
node canvas positions, pinned test data, editor metadata. If those fed the hash, dragging
a node two pixels would block every operation and operators would learn to re-hash
reflexively — which is the same as not checking at all.

So canonicalization must drop something. The question closed here is *how we decide what*,
and the answer matters more than it looks. The two failure directions are not symmetric:

- **Over-inclusion** (canonicalizing too little) produces false `DEFINITION_DRIFT`. Noisy,
  annoying, and *safe* — it fails toward refusing to run.
- **Over-exclusion** (canonicalizing too much) produces a **silent false negative**: a
  semantic change to the workflow that the hash does not notice, so the drift check passes
  and Operator executes a graph nobody reviewed. That is precisely the attack B8 exists to
  stop, re-introduced by a well-meaning cleanup.

Phase 0 listed this as the unresolved question most likely to be wrong and most expensive
to discover late. It also assumed a specific exclusion set (`updatedAt`, positions, pin
data) on no evidence at all — an assumption presented as a fact, which is exactly the
habit this decision exists to break.

## Decision

**Canonicalization is conservative and evidence-driven: every field is included by
default, and a field is excluded only after an empirical harness proves that changing it
cannot change workflow behavior.**

Seven rules, normative in BUILD_PLAN section 6.8 as **CAN-01** through **CAN-07**:

| Rule | Statement |
|---|---|
| **CAN-01** | **Inclusion by default.** Every field present in the n8n workflow definition contributes to the canonical form unless it appears on the exclusion allowlist. An unrecognized or newly-introduced field is included — never silently dropped because it was not anticipated. |
| **CAN-02** | **Exclusion requires proof.** A field may join the exclusion allowlist only after the compatibility harness (below) demonstrates that varying it, with everything else held constant, does not alter observable workflow behavior. |
| **CAN-03** | **The allowlist is explicit and justified.** It is an enumerated, versioned table in code. Each entry records the field path, the harness run that justified it, and the n8n version range the evidence covers. No wildcards, no regex families, no "anything under `meta`". |
| **CAN-04** | **Deterministic serialization.** Object keys sorted by Unicode code point; array order preserved and significant; strings NFC-normalized; numbers in a single canonical form; UTF-8, no insignificant whitespace. Canonicalization is idempotent. |
| **CAN-05** | **Semantic changes must change the hash.** Node type, node parameters, credential bindings, connections, workflow settings, trigger configuration, and error-handling configuration are semantic by definition and are never excludable, regardless of harness results. |
| **CAN-06** | **Only proven-cosmetic changes may preserve the hash.** A change that preserves the hash must correspond to an allowlist entry justified under CAN-02. There is no third category. |
| **CAN-07** | **Canonicalization is versioned.** The canonicalization algorithm version is part of the hash preimage (domain separation). Changing the algorithm therefore changes every hash, so it requires a new registry `apiVersion` and a deliberate re-hash — never a silent revaluation of existing entries. |

### The compatibility harness (phase 4)

A `live_n8n`-marked harness, opt-in and not run in CI:

1. Take a seed workflow definition and a candidate field path.
2. Emit two variants differing **only** in that field.
3. Install both on a live n8n instance and execute each against an identical input corpus.
4. Compare observable behavior: outputs, node execution sequence, per-node status,
   downstream side effects, and error surfaces.
5. Identical across the corpus and across the supported n8n version range ⇒ the field is a
   candidate for exclusion, recorded under CAN-03. Any divergence ⇒ it stays included,
   permanently, and the negative result is recorded too.

Absence of evidence is not evidence of cosmetic-ness: a field nobody has tested stays in
the hash. The harness can only ever *remove* noise, never *add* blindness.

### Fixtures

Every harness run saves a **sanitized** fixture under `tests/fixtures/canonicalization/`:
the two variant definitions, the canonical forms, the resulting hashes, the n8n version,
and the verdict. Sanitization strips instance URLs, credential identifiers, real workflow
IDs, and any payload data, per ADR-006. Fixtures make the offline unit tests real — they
assert against definitions that actually came from n8n — and they make a future
canonicalization change reviewable as a diff of hashes over a known corpus.

### What ships before the evidence exists

Phase 4 begins with an **empty exclusion allowlist**: nothing is dropped, and the hash
covers the definition as n8n returns it. Entries are added one at a time as the harness
justifies them. Early operators may see drift from cosmetic edits; that is the safe
direction, and it is temporary. Shipping with the *assumed* exclusion set would invert the
risk permanently.

## Consequences

### Positive

- The drift check cannot be quietly weakened. Every reduction in coverage is a reviewable
  code change carrying evidence (CAN-02, CAN-03).
- The dangerous failure is designed out. Over-exclusion requires a false harness result,
  not merely an oversight or an unanticipated n8n field (CAN-01).
- New n8n versions fail safe. A field added by an n8n upgrade is included automatically
  and produces visible drift rather than invisible blindness.
- Canonicalization becomes testable offline against real fixtures rather than invented ones.
- CAN-07 makes an algorithm change an explicit migration instead of a silent mass
  re-interpretation of every registered hash.

### Negative

- Phase 4 costs more: a live-n8n harness, a version matrix, and a fixture corpus, before
  the first field can be excluded.
- Early false-positive drift is real friction, and friction on a security control trains
  operators to bypass it. Mitigated by the harness landing in the same phase, by
  `registry hash` making re-hashing one command, and by v2's `diff_workflow_definition`
  making review a diff rather than a re-read.
- The allowlist is n8n-version-scoped, so it needs maintenance as n8n evolves.
- We will ship v1 with a smaller exclusion set than a naive implementation would have, and
  it will look like an unfinished feature to anyone who has not read this ADR.

### Neutral

- The exclusion set is expected to converge on roughly the fields Phase 0 assumed. The
  difference is that each will be there on evidence, and any that turn out to be
  behaviourally significant will have been caught rather than trusted.
- Sanitized fixtures are safe to commit and are the natural corpus for the v3 evaluation
  lab and the v3 workflow compiler's round-trip tests.

## Alternatives considered

**Ship the assumed exclusion set (`updatedAt`, positions, pin data) and revisit.** What
Phase 0 implied. Rejected: it inverts the risk asymmetry from day one, and the failure it
risks is silent. Pin data in particular is not obviously inert — pinned node output can
change what downstream nodes receive in some execution modes, which is exactly the sort of
thing an assumption gets wrong.

**Hash the whole definition with no canonicalization.** Maximally safe and unusable:
`updatedAt` alone would invalidate the hash on every save, so operators would re-hash
constantly and the check would become a formality. Rejected because a control people route
around is worse than a weaker control they respect.

**Semantic diffing instead of hashing** — compare structures and classify changes as
cosmetic or semantic at check time. Rejected for v1: it moves a security decision from a
constant-time comparison into a classifier with its own failure modes, and it is the wrong
shape for a gate ([ADR-007](ADR-007-deterministic-before-llm.md)). v2's
`diff_workflow_definition` presents a diff *to a human* after the hash has already failed
closed — advisory, not deciding.

**Operator-configurable exclusions in the registry.** Rejected outright: it lets the
person under drift-check pressure disable the drift check, per workflow, in the file they
already control. The exclusion allowlist stays in code, under review, with evidence.
