# ADR-013: Organization, tenant, and principal model

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-001](ADR-001-portable-mcp-core.md), [ADR-002](ADR-002-default-deny-registry.md), [ADR-014](ADR-014-oidc-trust-and-session-model.md), [ADR-015](ADR-015-rbac-authorization-evaluation.md), [ADR-016](ADR-016-environment-registry-overlays.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 3, 8, [THREAT_MODEL.md](../THREAT_MODEL.md) T-14

## Context

v1 has exactly one local principal (`local`) and nothing to isolate it from. v2's outcome
statement promises "a team can operate multiple n8n environments under role-based access
control" — which requires an actual isolation boundary between teams before RBAC has
anything meaningful to scope *within*. Left unspecified, "organization" would be invented
ad hoc inside the identity or environment work (stages 02–04) and would very likely
disagree with itself across those stages, because each would need an answer to "who can
see this row" without a shared definition of "who."

Three questions have to be settled before any v2 stage can start:

1. What is the isolation boundary, and what belongs to it?
2. Can one human hold more than one identity across organizations, or does the system
   assume one-org-per-person?
3. How does a human principal differ from a service (machine) principal, operationally
   and in the schema?

## Decision

**The organization is the tenant. Every row that can be scoped to a team is scoped to
exactly one organization, and a principal's relationship to an organization is a
membership, not an attribute of the principal.**

### 1. Organizations are the isolation boundary

An `organizations` table (`id`, `name`, `created_at`) is the unit of isolation.
Everything a v2 outcome statement implies belongs to a team — environments, registry
overlays, approval routing groups, notification/alert-hook subscriptions, metrics, and
audit query scope — carries an `organization_id` and is never visible across
organizations, full stop. There is no "global admin" query that spans organizations in
v2; that is out of scope until v3 enterprise controls, and inventing one now would be a
boundary this ADR is explicitly trying to draw.

The `workflows` registry entry itself (BUILD_PLAN section 6.2) stays organization-scoped
too: a workflow's base contract (title, description, `input_schema`, `risk`,
`side_effects`) belongs to one organization, with per-environment overlays
([ADR-016](ADR-016-environment-registry-overlays.md)) varying trigger wiring and
approval strength within that organization's own environments — never across
organizations.

### 2. Membership is many-to-many, with roles on the membership, not the principal

```
principals            organization_memberships           organizations
├─ id                  ├─ id                              ├─ id
├─ kind                ├─ principal_id  ──FK──┐            ├─ name
├─ display_name        ├─ organization_id ──FK─┼───────────┘
├─ external_subject    ├─ roles[]  (viewer|operator|approver|admin, ≥1)
├─ external_issuer     ├─ created_at
├─ disabled_at         └─ removed_at (null while active)
└─ created_at
```

**One OIDC subject may belong to multiple organizations.** A `principals` row is
identity, full stop — it does not carry a role or an organization. Role grants
(`organization_memberships.roles`, a non-empty set drawn from the four roles
[ADR-015](ADR-015-rbac-authorization-evaluation.md) defines) live on the membership row,
so the same human is legitimately `admin` in one organization and `viewer` — or nothing
at all — in another.

**Active organization selection has no separate "switch org" step.** Every v2 tool that
touches organization-scoped data resolves its organization *through* the `environment`
argument every v1 tool gains in v2 ([BUILD_PLAN.md](../BUILD_PLAN.md) section 7.2,
[MCP_TOOLS.md](../MCP_TOOLS.md) section 5) — each environment belongs to exactly one
organization, so naming an environment names an organization. `whoami` and
`list_environments` are the two exceptions: both operate across every organization the
caller belongs to, precisely so a caller can discover what to name before naming
anything (see their contracts in MCP_TOOLS.md section 5). There is no MCP tool that
lets a caller assert "act as organization X" independent of an environment — that
would be a second, easy-to-forget authorization axis, and this design keeps there
being exactly one.

### 3. Human and service principals are the same table, different `kind`

`principals.kind` is `user` or `service` (the v1 `local` value is retired at the v2
`apiVersion`, per the same rule ADR-008's CAN-07 uses for canonicalization — a version
bump, not a silent revaluation). Both kinds share every column; two are conditional:

- **`user`**: `external_subject` + `external_issuer` are both non-null and, together,
  the identity anchor ([ADR-014](ADR-014-oidc-trust-and-session-model.md) — `sub` alone
  is not safe to trust across issuers). A `user` principal is provisioned just-in-time on
  first successful authentication, but **never** with any organization membership —
  membership is granted separately, by an existing `admin`, out of band from
  authentication (see the JIT-provisioning boundary in ADR-014 section 3).
- **`service`**: `external_subject`/`external_issuer` are null; a service principal is
  created explicitly by an `admin` and carries a rotatable credential reference (never a
  literal secret — the same `env:`/`keyring:` indirection rule ADR-006 already requires
  for n8n credentials, rule R6, applies here). `disabled_at`, non-null, immediately fails
  authorization for every subsequent call — checked at evaluation time on every request,
  never cached (ADR-014 section 4).

`display_name` and `created_at` are common to both. Nothing about the operation state
machine, the handle model, or the audit chain distinguishes principal kinds — a
`service` principal preparing and executing an operation looks exactly like a `user`
principal doing the same, which is deliberate: automation is a first-class caller, not a
workaround.

## Consequences

### Positive

- Organization isolation is a data shape (a foreign key everywhere it matters), not a
  scattered set of `WHERE organization_id = ?` clauses an engineer has to remember to
  add — the same "authority is a data structure, not a code path" commitment ADR-002 and
  ADR-003 already make for the registry and handles (ARCHITECTURE.md section 1).
- Multi-organization membership falls out of the schema for free — no special-casing
  needed for a consultant or vendor who legitimately works across several teams'
  Operator deployments from one identity.
- Reusing the `environment` argument as the organization-selection mechanism means v2
  adds no new required argument to every tool call beyond the one already planned; a
  caller who has never heard the word "organization" still works correctly.
- JIT principal provisioning without JIT membership closes an obvious privilege-escalation
  path: an attacker who can obtain *any* valid token from the configured IdP gets a
  `principals` row and nothing else. They still see nothing until an `admin` grants them
  a membership.

### Negative

- A caller with zero memberships anywhere is a real, expected state (a freshly
  provisioned `user` principal before any admin has granted access) — every v2 tool must
  handle "authenticated, but a member of nothing" as a normal case, not treat it as an
  error worth surfacing differently than "member of something, but not authorized for
  this specific call" (both resolve to the same not-found-shaped errors,
  [ADR-015](ADR-015-rbac-authorization-evaluation.md)).
- Deriving organization from `environment` means `whoami`/`list_environments` genuinely
  are special — two tools with a different resolution rule than the other eighteen. That
  asymmetry has to be documented clearly enough that it does not read as an oversight
  (MCP_TOOLS.md section 5.1–5.2).
- No cross-organization query exists in v2, including for the party operating the
  Operator deployment itself. An operator running Operator for multiple client
  organizations gets no built-in way to see aggregate load across all of them without
  querying each organization's data separately. Accepted as an explicit v2 boundary,
  revisited only if v3 enterprise controls need it.

## Alternatives considered

**Organization as an attribute of a role grant, with no `organizations` table.**
Rejected: isolation would then depend on every query remembering to filter by a value
that lives on the grant rather than being enforced by a foreign key everywhere it
matters — exactly the "code path, not data structure" failure mode ADR-002 and ADR-003
already rejected for the registry and handles.

**One organization per principal (register a new identity per team, as many SaaS
products do).** Rejected: a consultant or vendor legitimately works across several
organizations' deployments from one real identity; forcing duplicate `principals` rows
per organization would also collide with the `(external_issuer, external_subject)`
identity anchor [ADR-014](ADR-014-oidc-trust-and-session-model.md) needs to stay unique.

**An explicit "switch active organization" tool or session step.** Rejected: it is a
second authorization axis independent of `environment`, and a caller (or an attacker)
forgetting to switch — or switching and forgetting they did — is exactly the kind of
confused-deputy surface ARCHITECTURE.md section 1 already commits to avoiding.

**JIT-provision a default membership on first successful authentication, so a new
principal is immediately useful.** Rejected: it converts "holds any valid token from
the configured IdP" into "has standing in some organization," which is a privilege
escalation path an attacker with IdP access could exploit trivially. Membership must be
an explicit `admin` act, never a side effect of logging in.
