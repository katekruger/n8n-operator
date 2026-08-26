# ADR-002: Default-deny YAML workflow registry

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) section 6, [THREAT_MODEL.md](../THREAT_MODEL.md) T-01, T-02, T-28

## Context

n8n exposes its workflows through an API. The obvious product is a proxy: list what is
there, describe it, run it. That product is a remote-execution primitive with an LLM
holding the trigger, and its permission model is "whatever is on the instance."

Real instances accumulate. Half-finished experiments sit next to production billing
workflows. A workflow named `test` may delete a table. Nothing in the n8n data model
declares blast radius, and nothing declares an input contract — the accepted shape is
implicit in the node graph and knowable only by reading it.

There is also a timing problem. Even if an operator reviews a workflow once, the
workflow can change afterwards, and a dynamic listing would silently pick up the change.

## Decision

**Only workflows listed in an operator-authored YAML registry are visible or executable.
Everything else does not exist. The registry is a static allowlist, not a filter over a
dynamic listing.**

1. `list_workflows` returns registry entries. It never enumerates the n8n instance.
2. `prepare_operation` resolves a registry ID against the active snapshot. A miss is
   `WORKFLOW_NOT_FOUND`, returned identically for unregistered and nonexistent IDs, so
   the error is not an enumeration oracle (AC-01, T-10).
3. Each entry carries what n8n cannot: a human-authored description, an explicit
   `input_schema`, a `risk` class, a `side_effects` class, an approval policy, limits,
   redaction paths, and a `definition_hash` pinning the reviewed graph.
4. Loading is all-or-nothing. Any violation of rules R1–R10 fails the load and the
   server refuses to start. There is no partially-live allowlist (AC-02).
5. The file is YAML, designed to live in version control and be reviewed in a pull
   request, and it contains no secrets (rule R6).

## Consequences

### Positive

- The permitted action set is finite, enumerable, and reviewable in a diff. "What can
  the agent do?" is answered by reading one file.
- Registering is a deliberate act with a checklist, not a side effect of a workflow
  existing (WORKFLOW_REGISTRY.md section 10).
- Input contracts become explicit and machine-checkable, which is what makes validation
  before dispatch possible at all.
- `definition_hash` closes the review-then-change gap: the reviewed graph is the graph
  that runs, or nothing runs (T-24, T-25).
- Prompt injection is bounded to the registered set. Injection can produce a request for
  a registered workflow; it cannot produce a request for one you never allowed (L-01).
- The registry is the extension mechanism, which is why the product needs no plugin
  system loading operator code into the trusted zone.

### Negative

- Manual work per workflow: read it, classify it, write a schema, compute a hash.
- The registry drifts from n8n unless maintained. Mitigated by `DEFINITION_DRIFT`
  surfacing drift loudly rather than silently tolerating it.
- Schema authoring is the highest-effort part of onboarding a workflow, and a
  badly-written schema is a bad contract enforced faithfully.
- No self-service discovery: adding a workflow requires an operator, by design.

### Neutral

- YAML over JSON or TOML for comments, multi-line descriptions, and readable diffs.
- v2 adds per-environment overlays; v3 may generate entries from compiled sources. The
  default-deny property is invariant across all three.

## Alternatives considered

**Dynamic listing with tag-based filtering** (expose workflows tagged `mcp-safe`).
Rejected: the allowlist lives in a system where anyone with n8n access can add a tag,
there is still no input schema or risk class, and it is fail-open — a mistagged
workflow is immediately live.

**Deny-list.** Rejected outright. Every new workflow is exposed by default; the operator
must predict what to forbid.

**Database-backed registry with a management UI.** Better ergonomics, worse reviewability:
changes to the security boundary happen in a UI without a diff, a reviewer, or history.
The registry is version-controlled precisely so registration is a reviewable commit.
Revisit if v2 multi-tenancy makes a file impractical — but the audit properties must be
preserved.
