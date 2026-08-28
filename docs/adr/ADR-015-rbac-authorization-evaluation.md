# ADR-015: RBAC authorization evaluation

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-002](ADR-002-default-deny-registry.md), [ADR-007](ADR-007-deterministic-before-llm.md), [ADR-013](ADR-013-organization-tenant-and-principal-model.md), [ADR-016](ADR-016-environment-registry-overlays.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 9 (boundary B1, T-10)

## Context

BUILD_PLAN section 3's feature-boundary table already names the four v2 roles
(`viewer`, `operator`, `approver`, `admin`) without saying what each one may do, and
already says authorization is "RBAC over tools, workflows, environments" without saying
how those three axes combine. Both are exactly the kind of decision that, left to whoever
implements a given tool in stages 02–08, will be answered differently tool by tool.

A second, sharper problem: v1's `WORKFLOW_NOT_FOUND` is not an enumeration oracle only
because there is nothing *to* distinguish in a single-tenant, single-role system — every
caller who reaches the server can see every registered workflow. v2 introduces callers
who are authenticated, real, and *still* not supposed to see a given workflow or
environment. Getting the not-found-versus-forbidden question wrong here reopens T-10
(BUILD_PLAN's own registered threat) in a form v1 never had to answer.

## Decision

**Four roles, each a strict superset of the one before it in read access. Authorization
is workflow-scope AND environment-scope AND role-capability — all three must pass, or
the result is the same not-found response an absent resource would produce.**

### 1. Role capability matrix

| Tool | `viewer` | `operator` | `approver` | `admin` |
|---|---|---|---|---|
| `list_workflows`, `describe_workflow`, `get_instance_health`, `validate_input`, `preflight_workflow` | ✓ | ✓ | ✓ | ✓ |
| `get_operation`, `list_operations`, `get_execution_result`, `get_execution_log` | ✓ | ✓ | ✓ | ✓ |
| `whoami`, `list_environments`, `diff_workflow_definition`, `get_metrics`, `list_audit_events` | ✓ | ✓ | ✓ | ✓ |
| `get_approval_status` | ✓ | ✓ | ✓ | ✓ |
| `prepare_operation`, `cancel_operation`, `execute_operation` | — | ✓ | — | ✓ |
| `request_approval` | — | ✓ | ✓ | ✓ |
| Out-of-band approve/reject decision (CLI or approval app, [ADR-010](ADR-010-approval-delivery-and-expiry.md); still **no MCP tool grants approval**, boundary B4) | — | — | ✓ | ✓ |
| `retry_operation` | — | — | — | ✓ |

Every role includes every read-only v1 and v2 tool — visibility is cheap and useful for
everyone with any standing in the organization; `viewer` exists so an organization can
grant "see everything, change nothing" without also granting the ability to run
anything. `approver` deliberately does **not** include `prepare_operation` or
`execute_operation`: separating "can request approval routing / can decide" from "can
run things" means the same person is not routinely both the requester and a possible
decider for the same operation — see section 2 for why that separation has teeth.
`retry_operation` is `admin`-only: a retry is a fresh, policy-significant
re-authorization of something that already failed once
([ADR-012](ADR-012-governed-retry-and-audit-anchoring.md)), and requiring the higher
role for it is a deliberate extra check on a path that recreates a side-effecting
capability, not a claim that `operator`s are otherwise untrusted.

### 2. Workflow-level and environment-level scope intersect; they do not union

A role grant (`organization_memberships`, [ADR-013](ADR-013-organization-tenant-and-principal-model.md))
may additionally carry a **workflow scope** (a set of workflow-ID patterns, `*` meaning
all) and an **environment scope** (a set of environment IDs, similarly `*`-capable). The
effective permission for a call touching workflow *W* in environment *E* is:

```
allowed  ⟺  role capability includes the tool
         ∧  W matches the membership's workflow scope
         ∧  E is in the membership's environment scope
```

All three conjuncts must hold. A membership scoped to `operator` on `staging` for
`crm.*` workflows does not grant `operator` on `prod` for any workflow, and does not
grant anything at all for a `billing.*` workflow even on `staging`. There is no
mechanism to union two narrower grants into a broader one implicitly — an admin who
wants broader access grants a broader membership explicitly. This mirrors
[ADR-016](ADR-016-environment-registry-overlays.md)'s environment-overlay rule that
scoping may only narrow, never widen, applied to authorization instead of policy.

### 3. Denial is shaped exactly like absence

An authorization failure on any of the three conjuncts above returns the identical
response a nonexistent resource would: `WORKFLOW_NOT_FOUND` for a workflow-scope or
role-capability failure touching a specific workflow, `ENVIRONMENT_NOT_FOUND` for an
environment-scope failure. **There is no `FORBIDDEN` error code in v2.** A caller
cannot distinguish "this workflow does not exist in this organization" from "this
workflow exists, but you are not authorized to see it" — extending
[ADR-002](ADR-002-default-deny-registry.md)'s existing anti-enumeration guarantee
(AC-01, T-10) across the organization boundary instead of only across the
registered/unregistered boundary. This is why `approver` excluding `prepare_operation`
in the matrix above matters operationally, not just organizationally: an approver who
is not also an operator on a given workflow cannot even discover whether that workflow
exists via `describe_workflow` unless their `viewer`-tier grant separately covers it —
role and scope really are independent knobs.

### 4. Approval capability is a role, not a per-operation grant

`approver` is evaluated the same way as any other role capability (section 2) — it does
not additionally require the approver to be named on the specific operation's routing.
*Which* approvers a given operation is routed to, and how many of them must decide, is
[ADR-017](ADR-017-team-approval-quorum-semantics.md)'s concern, layered on top of "is
this principal an approver for this workflow and environment at all."

## Consequences

### Positive

- A single evaluation function (capability ∧ workflow-scope ∧ environment-scope) is the
  only place authorization logic exists, matching ADR-007's "every security gate is
  deterministic" — there is no per-tool authorization special case to keep in sync as
  new v2 tools are added in later stages.
- Reusing `WORKFLOW_NOT_FOUND`/`ENVIRONMENT_NOT_FOUND` means every v1 error-handling
  guidance a model was already given (MCP_TOOLS.md section 4: "call `list_workflows`")
  is still the right instinct in v2 — authorization failures do not need their own
  model-facing playbook.
- Excluding `prepare_operation`/`execute_operation` from `approver` closes an easy
  self-dealing shape at the role level, before
  [ADR-017](ADR-017-team-approval-quorum-semantics.md)'s per-operation exclusion even
  has to run.

### Negative

- No `FORBIDDEN` code anywhere means a legitimate caller who mistyped a workflow ID and
  a legitimate caller who is genuinely unauthorized get identical error text, which is a
  worse debugging experience for the honest mistake — accepted deliberately, the same
  trade-off AC-01 already makes in v1.
- The matrix has no per-workflow *action* granularity narrower than "operator" (e.g.
  there is no role that can `execute_operation` but not `prepare_operation`, or that can
  approve some workflows but not decide the routing). Revisit only if a real v2 team
  reports this as a concrete gap — inventing finer roles speculatively repeats the
  mistake this ADR exists to avoid.
- Workflow-scope patterns add a second place (besides the registry's own `id` field)
  where a typo silently under- or over-grants. Stage 03's implementation must validate
  a scope pattern against at least one real registry entry at grant time, or fail
  loudly, rather than accepting an unmatchable pattern silently.

## Alternatives considered

**A `FORBIDDEN` error code, distinct from not-found.** Rejected in section 3: it is
precisely the enumeration oracle T-10 already names as a threat, now reachable across
the organization boundary instead of only the registered/unregistered one. The cost is
worse debugging for honest mistakes, accepted as the same trade-off AC-01 makes in v1.

**Union workflow-scope and environment-scope grants across a principal's multiple
memberships in one organization, so the broadest of either axis applies.** Rejected:
union quietly turns two narrow, intentional grants into one broad, unintentional one —
an admin granting `operator` on `staging` for `crm.*` and separately `viewer` on `prod`
for `billing.*` must never accidentally produce `operator` on `prod` for `billing.*`.
Intersection per grant, never combination across grants, is the only shape that keeps
"what did the admin actually authorize" answerable by reading one row.

**Per-action roles finer than the four-role matrix (e.g. execute-but-not-prepare).**
Rejected for v1 of this ADR: no real v2 team has asked for it, and inventing roles
speculatively repeats the mistake this ADR exists to avoid — a role invented without a
concrete need tends to disagree with the need that eventually shows up. Revisit only
against an actual reported gap.

**Attribute-based access control (arbitrary policy expressions) instead of a fixed role
matrix.** Rejected as the v2 default: ABAC's expressiveness is also its cost — a policy
language is one more thing to audit for the same enumeration-oracle mistake this ADR
closes, and the four-role matrix already covers every v2 outcome named in BUILD_PLAN
section 2.2. Available to reconsider in v3 if a real deployment's needs outgrow it.
