# ADR-016: Environment registry overlays

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-002](ADR-002-default-deny-registry.md), [ADR-013](ADR-013-organization-tenant-and-principal-model.md), [ADR-015](ADR-015-rbac-authorization-evaluation.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 6

## Context

v2's outcome statement gives one concrete example — "`staging` auto-approves, `prod`
requires two approvers" — that already implies a workflow's *policy* varies by
environment while presumably meaning the same thing everywhere. Left unspecified, this
invites the registry schema to let an overlay override anything, including
`input_schema` or `side_effects` — which would mean the same registry ID means a
different contract depending which environment a caller happens to name, defeating the
entire point of `describe_workflow` being a stable answer a model can reason from.

A second unresolved question sits directly behind the outcome statement: does naming no
environment ever *mean* production, and does deleting an environment erase the history
of operations that ran against it.

## Decision

**A workflow's contract is environment-independent. An environment overlay may only
adjust how it is reached and how strongly it is gated — never what it promises to do or
accept. Production is never an implicit default once more than one environment exists.
Environments are archived, never deleted.**

### 1. What an overlay may and may not change

The base registry entry (BUILD_PLAN section 6.2) stays exactly as specified: `id`,
`title`, `description`, `owner`, `risk`, `side_effects`, `input_schema`, `tags`,
`output` — one contract, organization-scoped, environment-independent.

A per-environment overlay may override exactly:

| Field | May the overlay change it? |
|---|---|
| `n8n_workflow_id` | Yes — the same registry `id` legitimately points at a different n8n workflow per environment (a staging copy and a prod copy of "the same" automation). |
| `definition_hash` | Yes, necessarily — different n8n workflows have different definitions. |
| `trigger.path`, `trigger.secret_ref` | Yes — different environments have different webhook wiring. |
| `approval` policy, `limits` | **Only to strengthen.** An overlay may raise `approval` from `none` to `required`, or raise `limits.approval_ttl_seconds`/lower `limits.max_concurrent`, etc. It may never weaken relative to the base entry — a base entry requiring approval cannot have an overlay that auto-approves it. This is the same one-directional-only shape ADR-011's rule R11 already uses for `max_argument_bytes` (a workflow may lower the server ceiling, never raise it), applied to policy instead of a byte limit. |
| `input_schema`, `side_effects`, `risk`, `title`, `description` | **No.** These define the contract `describe_workflow` and `validate_input` answer from. Changing them per environment would mean the same registry ID is a different tool depending which environment happened to be named — unacceptable ambiguity for a model reasoning about what it is calling. |

A registry entry that needs genuinely different `input_schema` per environment is not
one workflow with an overlay — it is two workflows with two registry IDs. The overlay
mechanism is for *where and how strictly*, never *what*.

### 2. Environments belong to exactly one organization; overlays are keyed uniquely

`environments` (`id`, `organization_id`, `name`, `n8n_base_url` config ref,
`n8n_api_key` secret ref — same `env:`/`keyring:` indirection ADR-006 already requires,
`is_production` boolean, `archived_at` nullable). A `workflow_environment_overlays` row
is keyed by `(workflow_id, environment_id)` with a **database unique constraint** —
"workflow overlay conflicts" (two overlays claiming the same workflow in the same
environment) is therefore structurally impossible, not a runtime check that could be
skipped or raced. This is the same "authority is a data structure" commitment
([ARCHITECTURE.md](../ARCHITECTURE.md) section 1) applied to overlay resolution.

### 3. Default environment resolution, and why production is never the implicit one

- **Exactly one environment registered for an organization**: it is the implicit
  default for every v1-carried-forward tool call that omits `environment` — this is
  v1's own single-instance behavior, unchanged, and covers every organization that
  has not yet outgrown it.
- **Two or more environments registered**: `environment` becomes **required**. Omitting
  it is `ENVIRONMENT_REQUIRED`, not a guess. This holds *even if* exactly one of the
  environments is `is_production: true` and the rest are not — production is never
  selected as an implicit default merely because it is the only production environment;
  the caller must name it, deliberately, every time.
- The one case where a solo production environment *is* the implicit default is the
  degenerate single-environment case above, because there is nothing else it could mean
  — an organization running exactly one environment and marking it `is_production` gets
  v1's exact ergonomics back. This is stated explicitly here so it is never mistaken for
  an oversight: a solo environment being implicit is required for v1 parity; a
  production environment being implicit *while alternatives exist* is refused.

### 4. Environment deletion is archival, never erasure

`environments.archived_at`, set instead of a row delete. An archived environment:

- cannot be targeted by a new `prepare_operation` (`ENVIRONMENT_ARCHIVED` — a distinct
  code from `ENVIRONMENT_NOT_FOUND`, deliberately: within an organization the caller
  already belongs to, knowing "this used to exist and was retired" is not the kind of
  cross-boundary information [ADR-015](ADR-015-rbac-authorization-evaluation.md) treats
  as sensitive — `list_environments` may still surface archived entries to an `admin`
  wanting a full history);
- remains fully resolvable by every read/inspection tool
  (`get_operation`, `list_operations`, `get_execution_result`, `get_execution_log`,
  `list_audit_events`) forever, because historical operations carry
  `operations.environment_id` as a foreign key and an audit record referencing a
  vanished environment would be a broken promise to every acceptance criterion about
  audit completeness (AC-25).

## Consequences

### Positive

- A model calling `describe_workflow` gets one true answer regardless of which
  environment it later names for `prepare_operation` — no surprise contract drift by
  environment.
- The overlay unique constraint means "which environments does workflow *W* exist in"
  is a plain query, never a reconciliation problem.
- Refusing an implicit production default when alternatives exist directly prevents the
  most damaging version of "I meant staging" — a caller who forgets to specify gets a
  clear, immediate `ENVIRONMENT_REQUIRED` instead of a silently-selected environment
  that happens to be the wrong one.
- Archival keeps every acceptance criterion depending on audit completeness true
  regardless of how an organization's environment topology changes over its lifetime.

### Negative

- Two genuinely different workflows sharing "the same idea" across environments (e.g. a
  staging workflow deliberately built with a looser `input_schema` for easier manual
  testing) cannot be modeled as one overlaid registry entry — an operator has to author
  two registry IDs and accept that they are visibly two things. Accepted: the
  alternative (letting `input_schema` vary per environment) is a worse ambiguity for
  every caller of `describe_workflow`/`validate_input`.
- Archived environments accumulate forever (no purge path in v2, matching BUILD_PLAN
  section 8.2's existing "no delete path in v1 or v2" retention stance) — an
  organization with heavy environment churn accumulates archived rows indefinitely.
  Retention policy is explicitly a v3 enterprise control.
- The one-directional-only overlay rule for `approval`/`limits` means an organization
  that genuinely wants a *looser* policy in one environment than the base entry
  declares cannot express it as an overlay — they must lower the base entry's policy
  instead (which then applies everywhere unless overlaid stricter elsewhere). This is
  the intended shape: the base entry should already reflect the most permissive policy
  the operator is willing to grant anywhere.

## Alternatives considered

**Let an overlay change any field, including `input_schema` and `side_effects`.**
Rejected in section 1: it would make the same registry ID answer `describe_workflow`
differently depending which environment happened to be named, which defeats the whole
point of that tool being a stable contract a model can reason from without first
guessing an environment.

**A runtime uniqueness check for overlay conflicts instead of a database constraint.**
Rejected: a check that runs in application code is a check that can be skipped by a
different code path, raced under concurrent writes, or simply forgotten by a future
change. A unique constraint on `(workflow_id, environment_id)` makes the conflict
structurally impossible, matching ADR-002's and ADR-003's "authority is a data
structure" commitment.

**Default to the environment marked `is_production: true` when more than one
environment exists and `environment` is omitted.** Rejected in section 3: it is the
single most damaging silent default available — a caller who meant staging and forgot
to say so would get production instead of an error. `ENVIRONMENT_REQUIRED` costs one
extra round trip; the alternative costs a wrong environment's worth of side effects.

**Hard-delete an environment row when an organization retires it.** Rejected: every
historical operation carries `environment_id` as a foreign key, and deleting the row
would either cascade-delete audit-relevant history (violating AC-25's audit-completeness
guarantee) or leave a dangling reference. Archival preserves both the deletion intent
(no new work targets it) and the history.
