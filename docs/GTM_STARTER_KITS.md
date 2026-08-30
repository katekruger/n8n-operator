# GTM starter kits

A tour of [`examples/registry/starter-kits/gtm-starter-kits.yaml`](../examples/registry/starter-kits/gtm-starter-kits.yaml)
— eight sanitized, fully-annotated workflow entries covering the seven common GTM
automation patterns, plus one extra entry used only by the RevOps quorum journey below.
Every command in this document was run for real against the shipped fixture, with no
external SaaS, no credentials, and no live n8n instance — see
[Try it yourself](#try-it-yourself) to reproduce the same run.

Nothing here is production-ready as-is. `n8n_workflow_id`, `definition_hash`,
`secret_ref`, and `trigger.path` in every entry are sanitized placeholders — see
[What to customize](#what-to-customize) before pointing this at a real n8n instance.

## Categories

### Read-only pipeline and campaign reporting — `reports.pipeline_summary`

Reads aggregate pipeline figures by stage and date range. `side_effects: read_only`,
`approval: none` — the only side-effect class permitted to skip approval (rule R5).
Grant `viewer`. No idempotency key needed (nothing to deduplicate). No correlation
tracking needed (a read has nothing to reconcile against downstream).

### Lead / account enrichment — `mkt.enrich_leads`

Looks up firmographic data for a batch of lead IDs and writes results to an internal
enrichment cache — never to the CRM record directly, which keeps this `read_only` even
though it writes somewhere. Applying results to the CRM is a separate,
`external_write` call (`crm.sync_contact`). `approval: none`. Grant `operator` to
trigger it, `viewer` to read results. Redacts email/phone in the response — enrichment
results can carry real PII about the *enriched* records.

### CRM contact/account upsert — `crm.sync_contact`

Upserts one contact by email. `side_effects: external_write`, `approval: required`.
This is the workflow [ARCHITECTURE.md section 11.1](ARCHITECTURE.md#111-a-startup-gtm-engineer-operating-staging-and-production)'s
worked journey uses, and [`examples/environments/staging.yaml`](../examples/environments/staging.yaml)
already overlays it — staging tightens `rate_limit_per_minute` to 5 (the base registry
sets none). `trigger.correlation: response_envelope`, so a dispatch that returns
`UNKNOWN` can be reconciled exactly (see [RECONCILING_UNKNOWN.md](RECONCILING_UNKNOWN.md)).

### Lead routing / ownership update — `crm.assign_lead_owner`

Reassigns a lead's owning rep. `external_write`, `approval: required`. A wrong
assignment means a rep works a lead that was never theirs, or a lead sits unworked —
routing rules often differ meaningfully between staging and production, so treat a
production approval here as a real review, not a formality.

### Campaign audience sync — `mkt.campaign_sync`

Pushes an audience/segment definition to the marketing platform for one campaign —
membership is resolved server-side from filter criteria, so the member list itself
never appears in the call or its result (both are redacted anyway). Named in
[ARCHITECTURE.md sections 11.1 and 11.3](ARCHITECTURE.md#11-v2-user-journeys) but never
built as a runnable registry entry until this stage. `external_write`, `approval:
required`.

### Customer communication requiring approval — `comms.send_customer_email`

Sends one email to one customer address. `side_effects: irreversible`, `risk: high` —
which forces `approval: required` regardless of registry configuration (rule R10). No
CC/BCC field exists at all, intentionally, to keep the blast radius of one bad approval
small. `trigger.correlation: response_envelope` — "did it actually send" must be
answerable exactly.

### Data-quality check and exception report — `dq.flag_pipeline_exceptions`

Scans CRM pipeline records against a named validation rule (`missing_close_date`,
`duplicate_email`, `stale_stage`, `invalid_amount`) and returns the records that fail
it. `read_only`, `approval: none` — fixing a flagged record is a separate, explicit,
`external_write` call the report's reader decides to make.

### (Journey-only) Bulk CRM update, two-approver quorum — `crm.bulk_update_stage`

Not one of the seven named categories on its own — exists so the RevOps
two-person-approval journey below has something to run. Updates the pipeline stage on
every deal matching a segment filter. `external_write`, `risk: high`,
`limits.quorum_count: 2` — two distinct approvers, never the requester, never the same
person twice (see the journey below for exactly how this is enforced).

## What to customize

Before any of these entries is more than a demo:

- **`n8n_workflow_id`** — copy from your own n8n instance's workflow URL.
- **`definition_hash`** — run `n8n-operator registry hash --n8n-workflow-id <id>
  --workflow-id <registry-id>` once connected to a real environment.
- **`secret_ref`** — set the named environment variable (or keyring entry) the
  reference points at; never replace it with a literal value (rule R6).
- **`trigger.path`** — match your instance's actual webhook path.

## Try it yourself

No database, no credentials, no live n8n instance needed — this validates the YAML
against the real registry loader:

```bash
n8n-operator registry validate --path examples/registry/starter-kits/gtm-starter-kits.yaml
n8n-operator registry list --path examples/registry/starter-kits/gtm-starter-kits.yaml
n8n-operator registry show crm.bulk_update_stage --path examples/registry/starter-kits/gtm-starter-kits.yaml
```

## Journeys

Each journey below is the exact walkthrough in
[ARCHITECTURE.md section 11](ARCHITECTURE.md#11-v2-user-journeys), run for real against
this starter-kit registry in a scratch database. Everything shown was actually
executed; where a step needs a live n8n instance, that boundary is called out
explicitly rather than faked.

### Journey 1 — a startup GTM engineer operating staging and production

```bash
export N8N_OPERATOR_DATABASE_URL="sqlite+pysqlite:///$(pwd)/demo.db"
n8n-operator db init
n8n-operator registry reload --path examples/registry/starter-kits/gtm-starter-kits.yaml

n8n-operator identity bootstrap \
  --org-name "Acme GTM" \
  --admin-issuer "https://idp.example.com" \
  --admin-subject "admin@acme.example.com"
# -> Organization created: <org-id>

n8n-operator environment create --org <org-id> --name staging \
  --n8n-base-url-ref env:STAGING_N8N_BASE_URL --n8n-api-key-ref env:STAGING_N8N_API_KEY
n8n-operator environment create --org <org-id> --name production \
  --n8n-base-url-ref env:PROD_N8N_BASE_URL --n8n-api-key-ref env:PROD_N8N_API_KEY --production
```

Granting the engineer's role is where the real CLI's shape diverges from
ARCHITECTURE.md's prose in one important way, worth calling out plainly:
**one `identity add-membership` call is one membership** — one role set, one
`workflow_scope` glob, one `environment_scope` list, all shared across every role in
that call. ARCHITECTURE.md section 11.1 describes the engineer as holding "`operator`
scoped to `crm.*` in both environments and `approver` scoped to `mkt.*` in staging
only" — two independently-scoped roles on one identity. The current membership model
cannot express that split for a single principal in one grant; a second
`add-membership` call for the same principal is refused
("This principal already has an active membership in this organization; remove it
first"). In practice this means either widening scope to cover what every granted role
needs (the workaround below), or splitting the two roles across two distinct
principals when the scopes must genuinely differ. This gap is worth reading before
designing a real policy pack — see [LEAST_PRIVILEGE.md](LEAST_PRIVILEGE.md) for how
each of the three worked profiles handles it.

```bash
n8n-operator identity add-membership --org <org-id> \
  --issuer "https://idp.example.com" --subject "engineer@acme.example.com" \
  --display-name "Riley Engineer" \
  --roles operator,approver --workflow-scope "*" \
  --environment-scope "<staging-id>,<production-id>" -y
```

`identity preview-permissions` is a second, related gotcha: it always evaluates as if
`environment_id=None` (it has no `--environment-id` flag), and a membership whose
`environment_scope` is anything other than `*` can only be satisfied by
`environment_id=None` when the scope is exactly `*` (see
`core/authorization.py`'s own "Environment-scope today" note). Run it against the grant
above and it shows **everything denied** — not because the grant is broken, but
because this preview command cannot represent a call carrying a specific environment
argument, which is exactly the kind of call the grant above is meant to authorize. Widen
`--environment-scope` to `*` on a throwaway grant if you want to sanity-check role
capabilities in isolation; use the real, narrowed grant for anything that will actually
be used.

```bash
n8n-operator environment validate-overlay <staging-id> --path examples/environments/staging.yaml
n8n-operator environment reload-overlay <staging-id> --path examples/environments/staging.yaml
n8n-operator environment registry-diff <staging-id> \
  --path examples/registry/starter-kits/gtm-starter-kits.yaml
# -> crm.sync_contact: ... rate_limit_per_minute=10 -> rate_limit_per_minute=5
```

From here, `prepare_operation` for `crm.sync_contact` against `staging` (no overlay
approval override, so it still executes without waiting) and against `production`
(base registry requires approval, routed to the org's `crm.*` approvers) are MCP-only
calls — no CLI equivalent exists (boundary B4: `prepare_operation`/`execute_operation`
are tool calls a client makes, not commands an operator types). See
[MCP_CLIENT_RECIPES.md](MCP_CLIENT_RECIPES.md) for the literal tool-call JSON, and
[the environment differences section above](#crm-contactaccount-upsert--crmsync_contact)
for why staging and production resolve this call differently for the identical
workflow ID. This is the live-n8n boundary: dispatching for real requires a reachable
n8n instance at the resolved `n8n_base_url_ref`.

### Journey 2 — a RevOps team requiring two-person approval for a bulk CRM update

`crm.bulk_update_stage` carries `limits.quorum_count: 2`. Once an operation reaches
`PENDING_APPROVAL`:

```bash
n8n-operator operations approval-status <operation-id>
# -> quorum_count: 2, decided: 0, ready: false
n8n-operator operations request-approval <operation-id>
# notifies every eligible approver over the NotificationSink webhook — event type,
# operation ID, and a fetch reference only, never the bulk update's argument list

n8n-operator operations approve <operation-id> -y   # first eligible approver
n8n-operator operations approve <operation-id> -y   # second eligible approver
# -> quorum reached; operation moves APPROVED
```

The requester, even if they hold `approver` themselves, is structurally excluded from
their own request's eligible-approver list — `operations approve` on their own
operation is refused, not merely discouraged. A third approver deciding after quorum
is already reached changes nothing (already-decided operations reject a second
decision from the same principal with `APPROVAL_ALREADY_DECIDED`). Dispatching
(`execute_operation`) is again an MCP-only, live-n8n-only step from here.

### Journey 3 — marketing operations investigating drift or a failed enrichment run

A `viewer`-scoped analyst (`mkt.*`, both environments, no `operator`/`approver`) can
walk this whole journey with read-only tools:

```bash
n8n-operator identity add-membership --org <org-id> \
  --issuer "https://idp.example.com" --subject "analyst@acme.example.com" \
  --display-name "Sam Analyst" \
  --roles viewer --workflow-scope "mkt.*" --environment-scope "*" -y
```

From here, `list_audit_events` filtered to `mkt.enrich_leads`, `get_execution_log` on
the failing operation, `get_metrics` for `mkt.*` over a `24h` window, and
`diff_workflow_definition` on `mkt.campaign_sync` are all MCP tool calls — see
[MCP_CLIENT_RECIPES.md](MCP_CLIENT_RECIPES.md) for the exact sequence. `viewer` is
sufficient for every one of them; no write capability is needed to ask "did this
change" or "why did this fail." A workflow that runs rarely enough (`mkt.enrich_leads`
in a quiet 24h window) can show `get_metrics`' p95 as `null` with
`"reason": "insufficient_sample"` rather than a misleading number — the audit event
from `list_audit_events` is the right tool for that case instead.
