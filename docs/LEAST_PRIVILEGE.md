# Least-privilege role and scope guidance

> Stage 03 ([ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)). Three worked
> profiles for how to grant `viewer`/`operator`/`approver`/`admin` roles with
> `workflow_scope`/`environment_scope` in practice. All grants below use
> `n8n-operator identity add-membership` (see `--help` for every flag); each
> `workflow_scope` pattern must match at least one real registry entry at grant time,
> or the command refuses loudly (ADR-015's own stated requirement — see
> [OIDC_SETUP.md](OIDC_SETUP.md) for identity setup and `whoami` for what a caller sees
> once granted).

## 1. A small startup: one organization, broad grants

A five-person team running one n8n instance, one Operator deployment, everyone
touching most workflows. The overhead of narrow per-workflow scoping buys little here —
the team *is* the trust boundary.

```bash
# Bootstrap once.
n8n-operator identity bootstrap --org-name "Acme" \
  --admin-issuer https://idp.example.com --admin-subject founder@acme.com

# Everyone who runs things day to day.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject alice@acme.com --roles operator

# One or two people who can decide on higher-risk operations — never the same
# person as the one preparing them (ADR-015's own self-decision rule enforces this
# at call time, not just by convention).
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject bob@acme.com --roles approver,operator

# Anyone who just needs visibility (a support engineer, a manager).
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject carol@acme.com --roles viewer
```

Every grant above defaults to `workflow_scope=*`, `environment_scope=*` — the whole
registry, no environment restriction (Stage 04 territory once real environments exist).
This is the right default here: a five-person team narrowing scope per-workflow
mostly just adds friction without a real isolation need behind it.

## 2. A centralized RevOps team: one organization, narrow per-team workflow-scope grants

A larger org where RevOps owns CRM automation, Marketing owns campaign workflows, and
neither team should be able to run the other's — but both report through one Operator
deployment and one organization (multiple environments and per-team org isolation are
Stage 04+ territory; this profile is the workflow-scope-only version of that shape,
buildable today).

If workflow IDs are chosen with a team prefix (`crm.sync_contact`, `mkt.campaign_sync`
— the registry's own ID schema already allows dot-segmented tokens; see
[WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md) for authoring workflow entries),
`workflow_scope` glob patterns map directly onto team boundaries — this is a naming
convention an organization adopts, not something Operator enforces on its own:

```bash
# RevOps: operate on crm.* only.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject revops-lead@acme.com \
  --roles operator,approver --workflow-scope "crm.*"

# Marketing: operate on mkt.* only.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject mkt-lead@acme.com \
  --roles operator,approver --workflow-scope "mkt.*"

# A cross-functional ops lead who needs to see everything but run nothing directly.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject ops-lead@acme.com --roles viewer
```

`revops-lead` attempting `crm.sync_contact` succeeds; the identical call against
`mkt.campaign_sync` resolves to `WORKFLOW_NOT_FOUND` — indistinguishable from that
workflow simply not existing (invariant I14). This is the concrete shape AC-39 names:
"a role scoped to workflow set W1... is authorized... only when the call's workflow is
in W1" — never broadened by a role that happens to be powerful elsewhere.

`create-service-principal` registers a scheduled job or webhook relay's own identity
(authenticated by credential, never OIDC — ADR-013 section 2), but granting it an
organization membership currently requires `add-membership`'s `--issuer`/`--subject`
path, which is JIT-resolution for a `user` principal only; there is no CLI path yet to
grant a role directly to an existing `service`-kind principal by ID. Flagged here as a
known gap, not glossed over — track it against whichever stage adds service-principal
authorization scoping, rather than assuming today's CLI already supports it.

## 3. A Series C marketing/sales operations org: staged for multi-environment

A larger, more mature org that genuinely needs staging/production isolation, per-team
scoping, and an approval quorum — most of which is Stage 04 (`environments`,
`list_environments`, the `environment` tool argument, `ENVIRONMENT_REQUIRED`) and
Stage 05 (`request_approval`, real quorum routing) territory, not yet built. What's
buildable **today**, staged so it composes cleanly once those stages land:

```bash
# Narrow workflow-scope grants exactly as profile 2, per team.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject sales-ops@acme.com \
  --roles operator,approver --workflow-scope "sales.*"

# environment_scope is left at its default (*) deliberately — narrowing it today
# would deny every real call (Stage 03's own scoping decision: a v1 tool call has no
# `environment` argument yet, so a scoped environment_scope can never be satisfied by
# a real call — see docs/adr/ADR-015-rbac-authorization-evaluation.md and this
# codebase's own core/authorization.py module docstring). Once Stage 04 ships real
# environment rows and the `environment` argument, re-grant this membership with
# `--environment-scope <staging-env-id>` for staging-only operators and a separate,
# narrower grant for whoever should reach production — never the same grant for both.
```

The load-bearing point for this profile: **don't pre-narrow `environment_scope` before
Stage 04 exists to honor it** — a membership narrowed today to specific environment IDs
that don't exist yet would deny every call, since the evaluator can only satisfy a
narrowed `environment_scope` against a real `environment` argument no v1 tool carries
(`core/authorization.py`'s own documented scoping decision). Leave it at `*` and revisit
once Stage 04 lands `environments`/`list_environments`/the `environment` argument.

## Previewing a grant before or after making it

```bash
n8n-operator identity preview-permissions <principal-id>
n8n-operator identity preview-permissions <principal-id> --workflow-id crm.sync_contact
```

Runs every `(role, tool)` pair in ADR-015's matrix through the real evaluator against
that principal's real, active memberships — the same decision every real tool call
makes, read-only, changes nothing. Use it to sanity-check a grant before handing over
credentials, or to debug "why can't this principal do X" without guessing.
