# Operator guide

The clean-machine path end to end — one level more detailed than the README's own
Quickstart, which stays short and links here. Every command below was actually run,
timed, against a fresh checkout with no external SaaS and no credentials.

## Phase 1 — a safe, no-credentials demo (measured: under a minute)

```bash
export N8N_OPERATOR_DATABASE_URL="sqlite+pysqlite:///$(pwd)/demo.db"
n8n-operator db init
n8n-operator registry validate --path examples/registry/starter-kits/gtm-starter-kits.yaml
n8n-operator registry reload --path examples/registry/starter-kits/gtm-starter-kits.yaml
n8n-operator registry list --path examples/registry/starter-kits/gtm-starter-kits.yaml
n8n-operator registry show reports.pipeline_summary \
  --path examples/registry/starter-kits/gtm-starter-kits.yaml
```

Nothing here talks to n8n, a database beyond a local SQLite file, or the network. This
is the "what is this and does it work" proof — see
[GTM_STARTER_KITS.md](GTM_STARTER_KITS.md) for what each of the eight loaded workflow
entries means. `registry list`/`show` need `--path` even after `reload` — they read
the file directly, not the database snapshot `reload` just persisted (a gotcha worth
knowing up front rather than hitting cold).

## Phase 2 — a local staging environment (measured: under two minutes)

```bash
n8n-operator identity bootstrap --org-name "Acme GTM" \
  --admin-issuer https://idp.example.com --admin-subject admin@acme.example.com
# -> Organization created: <org-id>

n8n-operator environment create --org <org-id> --name staging \
  --n8n-base-url-ref env:STAGING_N8N_BASE_URL --n8n-api-key-ref env:STAGING_N8N_API_KEY
n8n-operator environment create --org <org-id> --name production \
  --n8n-base-url-ref env:PROD_N8N_BASE_URL --n8n-api-key-ref env:PROD_N8N_API_KEY --production
# -> Environment created: <staging-id> / <production-id>

n8n-operator identity add-membership --org <org-id> \
  --issuer https://idp.example.com --subject you@acme.example.com \
  --roles operator,approver --workflow-scope "*" \
  --environment-scope "<staging-id>,<production-id>" -y

n8n-operator environment validate-overlay <staging-id> \
  --path examples/environments/staging.yaml
n8n-operator environment reload-overlay <staging-id> \
  --path examples/environments/staging.yaml
n8n-operator environment registry-diff <staging-id> \
  --path examples/registry/starter-kits/gtm-starter-kits.yaml
```

`--n8n-base-url-ref`/`--n8n-api-key-ref` are always indirected references
(`env:VAR_NAME` or `keyring:service/account`) — Operator never accepts a literal
secret or URL here (ADR-016). You don't need `STAGING_N8N_BASE_URL` actually set in
your shell to reach this point; it's only resolved when something dispatches against
that environment for real. See [LEAST_PRIVILEGE.md](LEAST_PRIVILEGE.md) for how to
scope this grant more narrowly than "everything" once more than one person is
involved, and [GTM_STARTER_KITS.md](GTM_STARTER_KITS.md) for the one real gotcha this
phase runs into (why a second `add-membership` call for the same principal is
refused).

## Connecting a client and approving something

See the README's own "Connecting a client" and "Approving a pending operation"
sections for the short version, and [MCP_CLIENT_RECIPES.md](MCP_CLIENT_RECIPES.md) for
the full tool-call sequence a Claude or OpenAI-compatible client actually sends.

## When something goes wrong

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) is a decision tree from symptom to cause —
start there before re-reading this guide top to bottom.

## What's next

[GTM_STARTER_KITS.md](GTM_STARTER_KITS.md) walks the three v2 user journeys
(ARCHITECTURE.md section 11) against the registry you just loaded, with real commands
for everything reachable without a live n8n instance. [APPROVER_GUIDE.md](APPROVER_GUIDE.md)
is the short version aimed at whoever is on the other side of a `request_approval`
notification, not the person running the commands above.
