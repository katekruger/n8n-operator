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

## 3. A Series C marketing/sales operations org: separate sales ops, marketing ops, security, and legal approvers

A larger, mature org that genuinely needs staging/production isolation, per-team
workflow scoping, and a real approval quorum for its riskiest workflows — all of this
is buildable today (environments landed in Stage 04, quorum in Stage 05).

```bash
n8n-operator identity bootstrap --org-name "Acme Corp" \
  --admin-issuer https://idp.example.com --admin-subject admin@acme.com
# -> Organization created: <org-id>

# Real, separate staging and production environments — connection details are always
# indirected references, never a literal secret or URL (ADR-016 section 2).
n8n-operator environment create --org <org-id> --name staging \
  --n8n-base-url-ref env:STAGING_N8N_BASE_URL --n8n-api-key-ref env:STAGING_N8N_API_KEY
# -> Environment created: <staging-id>
n8n-operator environment create --org <org-id> --name production \
  --n8n-base-url-ref env:PROD_N8N_BASE_URL --n8n-api-key-ref env:PROD_N8N_API_KEY --production
# -> Environment created: <production-id>

# Sales ops: operate on crm.* in both environments, decide on it only in staging —
# see the caveat below on why this is granted as one broadened membership, not two
# independently-scoped roles.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject sales-ops@acme.com \
  --roles operator,approver --workflow-scope "crm.*" \
  --environment-scope "<staging-id>,<production-id>"

# Marketing ops: the mkt.* equivalent.
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject mkt-ops@acme.com \
  --roles operator,approver --workflow-scope "mkt.*" \
  --environment-scope "<staging-id>,<production-id>"

# Security and legal: approve high-risk, irreversible operations only — never operate
# them (no `operator` role at all, so they can never be the one preparing what they'll
# later be asked to approve).
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject security-lead@acme.com \
  --roles approver --workflow-scope "*" --environment-scope "<production-id>"
n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject legal-lead@acme.com \
  --roles approver --workflow-scope "comms.*" --environment-scope "<production-id>"
```

**One `add-membership` call is one membership**: one role set, one `workflow_scope`
glob, one `environment_scope` list, shared across every role in that call. It cannot
express "`operator` on `crm.*` in both environments, but `approver` on `crm.*` in
staging only" as two independently-scoped roles for the same principal — a second
`add-membership` call for a principal that already has an active membership in the
organization is refused outright ("remove it first"). In practice this means either
granting the roles a person needs with one scope broad enough to cover all of them (as
above — sales-ops's `operator`+`approver` share one `crm.*`/`{staging,production}`
grant), or splitting genuinely different scopes across distinct principals (as above —
security and legal are separate approver-only identities, not roles bolted onto an
existing operator). Track this against ADR-015 if per-role scoping within one
membership becomes a real requirement; today, design the org's principal list around
this constraint rather than around the scoping you'd ideally want.

**Quorum**: `crm.bulk_update_stage` in
[the GTM starter kits](GTM_STARTER_KITS.md#journey-only-bulk-crm-update-two-approver-quorum--crmbulk_update_stage)
sets `limits.quorum_count: 2` — two of sales-ops's/security's/legal's `approver`
principals (whichever are eligible for that workflow×environment) must decide before
the operation executes, and the requester is structurally excluded from their own
request's eligible list even if they hold `approver` themselves. Walk the full,
real CLI sequence — `operations approval-status`, `operations request-approval`,
two `operations approve` calls — in
[GTM_STARTER_KITS.md's RevOps journey](GTM_STARTER_KITS.md#journey-2--a-revops-team-requiring-two-person-approval-for-a-bulk-crm-update).

## Previewing a grant before or after making it

```bash
n8n-operator identity preview-permissions <principal-id>
n8n-operator identity preview-permissions <principal-id> --workflow-id crm.sync_contact
```

Runs every `(role, tool)` pair in ADR-015's matrix through the real evaluator against
that principal's real, active memberships — the same decision every real tool call
makes, read-only, changes nothing. Use it to sanity-check a grant before handing over
credentials, or to debug "why can't this principal do X" without guessing.

**Caveat for any membership narrowed to specific environments** (profile 3's own
grants above, for instance): this command has no `--environment-id` flag, so it always
evaluates as if no environment were named at all. A membership's `environment_scope`
can only be satisfied by "no environment named" when the scope is exactly the default
`*` (`core/authorization.py`'s own "Environment-scope today" note) — so previewing a
`crm.*`/`{staging-id,production-id}`-scoped grant shows **everything denied**, even
though the grant works correctly for a real MCP tool call carrying an actual
`environment` argument. This is a limitation of the preview command, not a sign the
grant is broken; don't "fix" a correctly-narrowed grant by widening
`environment_scope` to `*` just to make this preview show allowed tools.
