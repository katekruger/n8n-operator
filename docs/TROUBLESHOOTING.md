# Troubleshooting decision tree

Symptom-first, branching to the exact fix or the doc that has it. For the one failure
mode this page deliberately doesn't duplicate — an indeterminate operation outcome —
go straight to [RECONCILING_UNKNOWN.md](RECONCILING_UNKNOWN.md); everything else
starts here.

## The server won't start

1. Bind failure (`Address already in use`) → another process holds the port; pick a
   different one or stop the other process. Not Operator-specific.
2. `N8N_OPERATOR_DATABASE_URL` unset or unreachable → `n8n-operator db status` reports
   the resolved URL and current schema revision without starting the server; fix the
   URL or run `n8n-operator db init` first.
3. Startup fails with an Origin/CORS-shaped error → the MCP transport's allowed-origin
   configuration doesn't include the client's origin; see the server's own
   `--help` for the relevant flag, not a code change.
4. Anything else at startup → the error message names the failing check by design
   (config validation fails loudly, never silently falls back) — read it before
   searching further.

## A tool call returns `WORKFLOW_NOT_FOUND`

By design this is one error for three different underlying causes (invariant I14: no
enumeration of *why* something is denied) — check each in order:

1. **Typo in `workflow_id`** — `n8n-operator registry list` (with `--path`, or against
   the active DB snapshot once loaded) to see the real IDs.
2. **Not in the registry at all** — confirm with `registry show <id>`; if it's a real
   n8n workflow that was never registered, that's [T-01](THREAT_MODEL.md)'s own
   intended refusal, not a bug — register it first
   ([WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md)).
3. **Out of the caller's `workflow_scope`** — `n8n-operator identity
   preview-permissions <principal-id> --workflow-id <id>` shows whether this specific
   principal's grant covers it. A workflow that's `Denied` here for scope reasons looks
   identical, from the caller's own error, to a workflow that doesn't exist —
   deliberately (see [LEAST_PRIVILEGE.md](LEAST_PRIVILEGE.md)).

## An operation is stuck in `PENDING_APPROVAL`

1. Check the deadline: `n8n-operator operations approval-status <operation-id>` shows
   `approval_ttl_seconds` and when it expires. Past expiry, it moves to `EXPIRED`
   automatically — nothing to fix, just prepare a fresh operation.
2. Nobody was notified → was `request_approval` actually called?
   `prepare_operation` alone does not route or notify; `operations request-approval
   <operation-id>` does.
3. Quorum not reached → `approval-status` shows how many of the required distinct
   approvers have decided. If the requester is the only eligible approver visible,
   remember self-approval is structurally excluded — someone else needs the role.
4. An eligible approver's decision was rejected → they were either not in the
   snapshot taken at request time (a membership change after the fact doesn't apply
   retroactively) or had already decided once (`APPROVAL_ALREADY_DECIDED`).

## An operation is `BLOCKED`

`preflight_workflow`/`prepare_operation` found a reason not to proceed before ever
reaching approval or dispatch — the block reason is in the operation's own detail, not
hidden. Common causes: the environment is archived (`ENVIRONMENT_ARCHIVED`), the
registered `definition_hash` no longer matches the live n8n definition (drift — see
below), or an argument fails schema/limit validation
(`validate_input`/`preflight_workflow` will have already said which).

## `UNKNOWN` operation outcome

Don't guess, don't retry blind — go to [RECONCILING_UNKNOWN.md](RECONCILING_UNKNOWN.md)
directly; it's the full decision tree for exactly this state.

## Drift detected (`diff_workflow_definition` / `environment registry-diff` shows a change)

The registered `definition_hash` (or, for an overlay-touched field, the overlay
itself) no longer matches what's live on the instance. This is informational, not
automatically blocking, unless the workflow's own drift-alert configuration says
otherwise ([METRICS_AND_ALERTS.md](METRICS_AND_ALERTS.md)). Re-register the workflow
(new `definition_hash`, deliberate version bump) once you've confirmed the live change
is intentional — never silently accept drift by re-hashing without checking what
actually changed.

## Rate limited / argument too large

The exact limit and its source are always named in the error: a workflow's own
`limits.rate_limit_per_minute`/`limits.max_argument_bytes` (see the entry in
`registry show <id>`), or — when the entry leaves `max_argument_bytes` unset — the
server-wide ceiling (`--server-max-argument-bytes`, ADR-011). Not a bug to work around;
raise the registry entry's own limit deliberately if it's genuinely too tight for
legitimate use, and record why in the entry's own annotation (see
[GTM_STARTER_KITS.md](GTM_STARTER_KITS.md) for the convention).

## `anchor verify` fails

Full procedure, key rotation, and recovery steps live in
[AUDIT_ANCHORING.md](AUDIT_ANCHORING.md) — not duplicated here.

## Nothing above matches

Check [V1_LIMITATIONS.md](V1_LIMITATIONS.md) for a boundary that might explain the
behavior, then [THREAT_MODEL.md](THREAT_MODEL.md) if the behavior looks like a refusal
rather than a bug — a lot of "this doesn't work the way I expected" turns out to be a
deliberate refusal documented there.
