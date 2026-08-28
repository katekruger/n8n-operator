# Stage 04 prompt — multi-environment registry and policy

Copy this entire file into a fresh Claude Code session after Stage 03 is merged.

## Mission

Support named n8n environments and per-environment registry overlays without duplicating
workflow contracts or allowing production to be selected accidentally.

## Required work

- Implement the Stage 00 environment model, encrypted/indirected server-owned connection
  configuration, environment health adapter resolution, and organization ownership.
- Define deterministic base-registry plus environment-overlay merge semantics. Reject
  unknown keys, ambiguous overrides, attempts to weaken global safety invariants, literal
  secrets, duplicate live IDs, or incomplete environment bindings.
- Snapshot the fully resolved contract used for each operation so later overlay edits do
  not rewrite history.
- Add optional `environment` to every v1 tool and add `environment` to every result. Apply
  authorization before discovery, validation, preflight, operation access, and execution.
- Implement `list_environments` with safe health and approval-policy summaries. Never return
  instance URLs, raw workflow IDs, secret references, or hidden environment names.
- Make default-environment behavior explicit. Production must not become an invisible
  default unless Stage 00 expressly permits it with a safe, tested rule.
- Provide an environment CLI for validate, list, show-safe, health, and registry-diff. Add
  annotated `development`, `staging`, and `production` examples.

## GTM scenarios to prove

- A GTM engineer validates a campaign workflow in staging, then intentionally prepares the
  production equivalent under stricter approval and rate limits.
- A RevOps team exposes CRM enrichment in all environments but restricts bulk update to
  staging and production admins.
- A marketing operator cannot infer or address a sales-only production workflow.

Test environment-name collisions, missing overlays, disabled environments, different n8n
versions, workflow absent in one environment, drift in only production, connection failure,
default switching, stale snapshot use, idempotency keys reused across environments, and
cross-environment operation access.

## Completion gate

Both stores, both transports, every v1 tool, `whoami`, and `list_environments` must pass
multi-environment contract tests. Retain a no-secrets artifact inspection and a two-instance
integration harness. Return Stage 05 entry criteria.
