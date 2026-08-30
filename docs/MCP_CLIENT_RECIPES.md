# MCP client recipes

End-to-end tool-call sequences using only the shipped 20-tool v2 surface (12 v1 tools
+ 8 v2 tools — see [MCP_TOOLS.md](MCP_TOOLS.md) for the authoritative contract every
shape below is drawn from verbatim). For how to actually connect a client, see
[`examples/mcp-clients/`](../examples/mcp-clients/) — this page picks up from "already
connected" and shows what a Claude or OpenAI-compatible client actually sends and
receives for each step named in the stage 10 mission: discovery, validation,
preflight, approval routing, polling, execution, failure inspection, retry, diff
review, and audit/metrics investigation.

Every example below uses `crm.sync_contact` from the
[GTM starter kits](GTM_STARTER_KITS.md) — swap the `workflow_id` and `arguments` for
your own registered workflow.

## Discovery — `list_workflows`

```json
{"name": "list_workflows", "arguments": {"side_effects": "external_write"}}
```
```json
{"workflows": [{"workflow_id": "crm.sync_contact", "title": "Sync a contact into the CRM",
  "risk": "medium", "side_effects": "external_write", "approval": "required",
  "tags": ["crm", "contacts", "starter-kit"], "owner": "revops", "version": 3}],
 "registry_snapshot": "sha256:1a2b…", "count": 1}
```

## Validation — `validate_input`

Cheap self-correction before ever creating an operation:

```json
{"name": "validate_input",
 "arguments": {"workflow_id": "crm.sync_contact",
               "arguments": {"email": "not-an-email", "tier": "platinum"}}}
```
```json
{"valid": false, "errors": [
  {"path": "/email", "code": "FORMAT", "message": "Value is not a valid email address."},
  {"path": "/tier", "code": "ENUM", "message": "Value 'platinum' is not one of: free, pro, enterprise."}
]}
```

## Preflight — `preflight_workflow`

```json
{"name": "preflight_workflow", "arguments": {"workflow_id": "crm.sync_contact"}}
```
```json
{"ready": true, "checks": [
  {"check": "instance_reachable", "status": "pass"},
  {"check": "definition_unchanged", "status": "pass"},
  {"check": "correlation", "status": "pass"}
], "checked_at": "2026-08-30T14:03:11Z"}
```

A `fail` anywhere sets `ready: false` and is exactly what `prepare_operation` would
also refuse on — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#an-operation-is-blocked)
for what to do with each check code.

## Preparing and routing for approval — `prepare_operation` then `request_approval`

```json
{"name": "prepare_operation",
 "arguments": {"workflow_id": "crm.sync_contact",
               "arguments": {"email": "lead@example.com", "tier": "pro"},
               "idempotency_key": "sync-lead-example-2026-08-30"}}
```
```json
{"operation_id": "op_01JQ…", "state": "PENDING_APPROVAL", "workflow_id": "crm.sync_contact",
 "approval_required": true,
 "approval_instructions": "A human must approve this operation: run `n8n-operator operations approve op_01JQ…`. You cannot approve it yourself.",
 "approval_expires_at": "2026-08-30T14:18:11Z", "created_at": "2026-08-30T14:03:11Z",
 "idempotent_replay": false}
```

The client cannot approve this itself (boundary B4) — it can only ask a human to, and
optionally route/notify that request explicitly:

```json
{"name": "request_approval", "arguments": {"operation_id": "op_01JQ…"}}
```
```json
{"operation_id": "op_01JQ…", "quorum_count": 1,
 "approval_policy_snapshot": ["prin_01JA…"], "notified": ["prin_01JA…"],
 "state": "PENDING_APPROVAL"}
```

The actual decision happens out-of-band — see [APPROVER_GUIDE.md](APPROVER_GUIDE.md).

## Polling — `get_operation`

```json
{"name": "get_operation", "arguments": {"operation_id": "op_01JQ…"}}
```
```json
{"operation_id": "op_01JQ…", "workflow_id": "crm.sync_contact", "state": "APPROVED",
 "approval": {"required": true, "decided": true, "decision": "approved",
              "decided_at": "2026-08-30T14:05:02Z"},
 "handle_used": false, "arguments": {"email": "[REDACTED]", "tier": "pro"}}
```

Poll this — don't assume approval landed just because time passed; an expired
operation reads as `EXPIRED` here even before any background sweep.

## Execution — `execute_operation`

The only tool in the product that causes an external side effect:

```json
{"name": "execute_operation", "arguments": {"operation_id": "op_01JQ…", "handle": "hdl_9f2c…"}}
```
```json
{"operation_id": "op_01JQ…", "state": "SUCCEEDED", "started_at": "2026-08-30T14:05:40Z",
 "finished_at": "2026-08-30T14:05:42Z", "duration_ms": 2104,
 "result": {"contact_id": "c_8891", "created": false}}
```

An indeterminate outcome (`state: "UNKNOWN"`) is possible here — never retry it
blind; go to [RECONCILING_UNKNOWN.md](RECONCILING_UNKNOWN.md).

## Failure inspection — `get_execution_log`

```json
{"name": "get_execution_log", "arguments": {"operation_id": "op_01JR…"}}
```
```json
{"operation_id": "op_01JR…", "state": "FAILED",
 "nodes": [
   {"name": "Webhook", "type": "n8n-nodes-base.webhook", "status": "success", "duration_ms": 3},
   {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "status": "error",
    "duration_ms": 812,
    "error": {"type": "NodeApiError", "message": "Request failed with status 422", "http_status": 422}}
 ], "failed_node": "HTTP Request", "truncated": false}
```

## Retry — `retry_operation`

A fresh operation, fully re-validated and re-preflighted against the *current*
registry snapshot — never a raw re-dispatch of the parent's stale arguments:

```json
{"name": "retry_operation", "arguments": {"operation_id": "op_01JR…"}}
```
```json
{"operation_id": "op_01JZ…", "parent_operation_id": "op_01JR…", "state": "PENDING_APPROVAL",
 "workflow_id": "crm.sync_contact", "approval_required": true,
 "approval_expires_at": "2026-08-30T15:00:00Z", "created_at": "2026-08-30T14:45:00Z",
 "idempotent_replay": false}
```

`retry_operation` is `admin`-only (ADR-012) — a fresh, policy-significant
re-authorization, not something `operator` grants on its own.

## Diff review — `diff_workflow_definition`

```json
{"name": "diff_workflow_definition", "arguments": {"workflow_id": "mkt.campaign_sync", "environment": "production"}}
```
```json
{"workflow_id": "mkt.campaign_sync", "environment": "production",
 "registered_hash": "sha256:6b3d…", "live_hash": "sha256:9f8e…", "changed": true,
 "diff": [{"path": "/nodes/2/parameters/url", "change_type": "modified",
           "registered_value": "[REDACTED]", "live_value": "[REDACTED]"}]}
```

## Audit and metrics investigation — `list_audit_events` and `get_metrics`

```json
{"name": "list_audit_events", "arguments": {"workflow_id": "mkt.enrich_leads", "limit": 20}}
```
```json
{"events": [{"seq": 40231, "occurred_at": "2026-08-29T22:10:02Z", "actor": "prin_01JA…",
  "action": "operation.failed", "subject_type": "operation", "subject_id": "op_01JR…",
  "outcome": "failed", "detail": {}}], "next_cursor": "eyJzZXEiOjQwMjMxfQ"}
```
```json
{"name": "get_metrics", "arguments": {"group_by": "workflow", "window": "24h"}}
```
```json
{"window": "24h", "generated_at": "2026-08-30T18:00:00Z",
 "totals": {"count": 214, "by_outcome": {"succeeded": 190, "failed": 12, "unknown": 2, "blocked": 10}},
 "latency_ms": {"p50": 812, "p95": 2104, "p99": null, "p99_reason": "insufficient_sample"},
 "breakdown": [{"key": "crm.sync_contact", "count": 140, "by_outcome": {"succeeded": 138, "failed": 2}}]}
```

Both are pre-filtered to the caller's authorized workflow set *before* any aggregation
runs — a `viewer` scoped to `mkt.*` never sees another team's workflow in a total, even
an aggregate one. Full walkthrough with an authorization-scoped `viewer` principal:
[GTM_STARTER_KITS.md's marketing-investigation journey](GTM_STARTER_KITS.md#journey-3--marketing-operations-investigating-drift-or-a-failed-enrichment-run).
