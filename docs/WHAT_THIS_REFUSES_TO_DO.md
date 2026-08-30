# What this refuses to do

The README's short version of this list is the one most people should read first —
this page exists for the "why," and for a link into the exact threat this refusal
closes ([THREAT_MODEL.md](THREAT_MODEL.md)) for anyone deciding whether Operator fits
their own risk model.

## Expose a workflow that is not in the registry, however live it is on the instance

The registry is a default-deny allowlist (ADR-002): a workflow that exists in n8n but
was never registered is invisible to every tool, indistinguishable from a workflow
that does not exist at all. There is no "discover everything on the instance" mode and
none is planned — an MCP client's blast radius is bounded by what a human explicitly
listed, not by what the instance happens to contain.
Closes [T-01](THREAT_MODEL.md) ("caller invokes a workflow the operator never
registered").

## Accept a raw n8n workflow ID, URL, or payload in any tool argument

Every tool argument that identifies a workflow takes the registry's own `workflow_id`
— a value a human chose when they authored the entry, never an n8n-internal ID, a
webhook URL, or a raw payload passed straight through. This is enforced by schema, not
by a runtime check that could be skipped or bypassed.
Closes [T-02](THREAT_MODEL.md) ("caller supplies a raw n8n workflow ID, URL, or
webhook path to reach an unregistered workflow").

## Return a credential, token, n8n ID, or instance URL in any tool result

Response shapes are an explicit allowlist (`WorkflowSummary`, `WorkflowDetail`, and
their v2 equivalents) — fields like `n8n_workflow_id`, `secret_ref`, and the instance's
base URL simply do not exist on the types a tool call can return, so there is no
redaction step to forget. An MCP client cannot leak what it was never given.
Related to boundary B5 (response shaping) in [THREAT_MODEL.md](THREAT_MODEL.md).

## Let an MCP client approve its own operation — approval is out-of-band, always

No MCP tool can decide an approval (boundary B4). Approving happens through the CLI
(`operations approve`) or the approval web app, both requiring a human session — never
a tool call an agent can make on its own. Possessing an operation's approval URL is
not authority to use it: fetching it renders a page, and deciding requires a `POST`
from an authenticated human session with a CSRF token.
Closes [T-08](THREAT_MODEL.md) ("caller fetches the approval URL returned by
`prepare_operation` to approve itself") and its v2 analogue T-57 (forged/replayed
per-approver tokens).

## Retry anything automatically. Ambiguous outcomes surface as `UNKNOWN` for a human

When a dispatch's outcome cannot be determined (a timeout, a dropped connection after
the request left Operator), the operation moves to `UNKNOWN` and stays there — never
silently retried, never guessed at. A human reconciles it explicitly
(`operations reconcile`, [RECONCILING_UNKNOWN.md](RECONCILING_UNKNOWN.md)) or triggers
a fresh, freshly-authorized retry (`retry_operation`, `admin`-only) — Operator itself
never assumes an indeterminate write either happened or didn't.

## Edit workflows (v1 and v2). Authoring stays in the n8n UI

Operator reads workflow definitions (to diff them against the registry) and dispatches
their configured trigger — it has no tool, CLI command, or code path that writes back
to a workflow's own definition in n8n. Authoring and editing a workflow's logic is
always done directly in the n8n UI, outside Operator entirely.

## Grant a role wider than what was explicitly asked for

`add-membership` refuses a broad grant (`workflow_scope: "*"` or
`environment_scope: "*"`) without an explicit `--yes`/`-y` confirmation — a typo'd
scope pattern that would silently match "everything" is caught at grant time, not
discovered later. See [LEAST_PRIVILEGE.md](LEAST_PRIVILEGE.md) for what a
deliberately-scoped grant looks like across three worked org sizes.

## Pretend a starter kit is production-ready

Every entry in [`examples/registry/starter-kits/gtm-starter-kits.yaml`](../examples/registry/starter-kits/gtm-starter-kits.yaml)
uses a sanitized, fake-shaped `n8n_workflow_id` and `definition_hash` — loading it
without customizing both (plus `secret_ref` and `trigger.path`) against a real n8n
instance is loud, not silent: `definition_hash` mismatches on the very first
`diff_workflow_definition`/`registry-diff` call. See
[GTM_STARTER_KITS.md](GTM_STARTER_KITS.md#what-to-customize).
