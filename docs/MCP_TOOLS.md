# n8n Operator — MCP Tool Contracts

> Normative for tool names, arguments, results, and errors. The **inventory** (which
> tools exist in which version) is normative in [BUILD_PLAN.md](BUILD_PLAN.md) section 7;
> this document specifies the contracts of the v1 tools and sketches v2 and v3.
> State names are those of BUILD_PLAN section 5.1.

---

## 1. Conventions

- **Identifiers.** Tools accept `workflow_id` (a registry ID) and `operation_id`
  (`op_<ULID>`). No tool accepts an n8n workflow ID, an instance URL, a webhook path,
  or a raw request body. This is enforced by the argument schemas, not by validation
  code (boundary B1).
- **Argument schemas.** Every tool's arguments are a Pydantic v2 model exported as
  JSON Schema draft 2020-12 with `additionalProperties: false`. Unknown fields are a
  hard error, never ignored.
- **Result shaping.** Every result is projected onto an explicit field allowlist before
  serialization. Fields not listed here are not returned, including newly added
  internal fields (boundary B5).
- **Timestamps.** RFC 3339, UTC, e.g. `2026-08-25T14:03:11Z`.
- **Redaction.** Any value matched by a workflow's `output.redact` paths is replaced
  with the literal string `"[REDACTED]"`. Truncated payloads carry `"truncated": true`.
- **No credentials, ever.** No result field in any version contains an API key, webhook
  secret, bearer token, n8n workflow ID, or n8n instance URL.
- **Side effects.** Exactly two v1 tools change state in the outside world:
  `prepare_operation` (creates an operation) and `execute_operation` (runs the workflow).
  `cancel_operation` terminates an operation but touches nothing outside Operator. All
  others are pure reads of Operator state — with one caveat below.
- **Lazy expiry.** Every tool that reads or acts on an operation first applies any overdue
  T08 or T11 transition in the same transaction, so an operation past its deadline always
  reads as `EXPIRED` regardless of whether a sweeper is running (invariant I9,
  [ADR-010](adr/ADR-010-approval-delivery-and-expiry.md)). "Pure read" therefore means
  "changes nothing but an already-overdue deadline".
- **Approval reachability.** An `approval_url` is returned only to callers the transport
  proves are local. Remote callers get `approval_required`, the operation ID, and
  human-readable instructions instead (invariant I12, boundary B13).

---

## 2. v1 tools

### 2.1 `list_workflows`

Discover the registered, enabled workflows. This is the entire universe of what can be
run — a workflow live on the n8n instance but absent from the registry is not listed
and cannot be prepared ([ADR-002](adr/ADR-002-default-deny-registry.md)).

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `tags` | array of string | no | Return only workflows carrying **all** listed tags. |
| `risk` | enum | no | `low` / `medium` / `high`. |
| `side_effects` | enum | no | `read_only` / `external_write` / `irreversible`. |

**Result**

```json
{
  "workflows": [
    {
      "workflow_id": "crm.sync_contact",
      "title": "Sync a contact into the CRM",
      "description": "Upserts one contact by email...",
      "risk": "medium",
      "side_effects": "external_write",
      "approval": "required",
      "tags": ["crm", "contacts"],
      "owner": "carolyn",
      "version": 3
    }
  ],
  "registry_snapshot": "sha256:1a2b…",
  "count": 1
}
```

**Errors:** `REGISTRY_UNAVAILABLE`.

---

### 2.2 `describe_workflow`

The full contract for one workflow: everything a model needs to construct a valid call
and to understand what approving it would mean.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `workflow_id` | string | yes |

**Result**

```json
{
  "workflow_id": "crm.sync_contact",
  "title": "Sync a contact into the CRM",
  "description": "Upserts one contact by email. Creates if absent, updates if present.",
  "owner": "carolyn",
  "version": 3,
  "risk": "medium",
  "side_effects": "external_write",
  "approval": "required",
  "tags": ["crm", "contacts"],
  "input_schema": { "type": "object", "additionalProperties": false, "…": "…" },
  "output": { "max_bytes": 65536, "include_node_trace": false, "redacted_paths": 2 },
  "limits": {
    "timeout_seconds": 30,
    "approval_ttl_seconds": 900,
    "execution_ttl_seconds": 300,
    "max_concurrent": 1,
    "rate_limit_per_minute": null,
    "max_argument_bytes": null
  },
  "registry_snapshot": "sha256:1a2b…"
}
```

`output.redacted_paths` is a **count**, not the paths themselves — publishing the paths
would tell an attacker exactly which fields are worth attacking.

`limits.max_argument_bytes` is `null` unless this workflow lowers the server's own
argument-size ceiling (ADR-011); a workflow may only lower the ceiling, never raise it.

**Errors:** `WORKFLOW_NOT_FOUND`, `REGISTRY_UNAVAILABLE`.

---

### 2.3 `get_instance_health`

Whether the configured n8n instance is reachable. Returns no URL and no credential.

**Arguments:** none.

**Result**

```json
{
  "reachable": true,
  "n8n_version": "1.―.―",
  "latency_ms": 42,
  "checked_at": "2026-08-25T14:03:11Z"
}
```

When unreachable: `{"reachable": false, "reason": "INSTANCE_UNREACHABLE", "checked_at": "…"}`.
The reason is a code, never a raw connection error containing the host.

**Errors:** none — unreachability is a result, not an error.

---

### 2.4 `validate_input`

Check arguments against a workflow's input schema without creating an operation or
touching n8n. Intended as the model's cheap self-correction loop.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `workflow_id` | string | yes |
| `arguments` | object | yes |

**Result**

```json
{
  "valid": false,
  "errors": [
    { "path": "/email", "code": "REQUIRED", "message": "Field 'email' is required." },
    { "path": "/tier", "code": "ENUM",
      "message": "Value 'platinum' is not one of: free, pro, enterprise." },
    { "path": "/nickname", "code": "ADDITIONAL_PROPERTY",
      "message": "Unknown field 'nickname'; this workflow accepts no extra fields." }
  ]
}
```

`path` is a JSON Pointer into the submitted arguments. Every error names the offending
location and the correction, so a model can repair the call without guessing (AC-04).

**Errors:** `WORKFLOW_NOT_FOUND`.

---

### 2.5 `preflight_workflow`

Check that the workflow could run right now, without creating an operation. Runs the
same checks `prepare_operation` runs, so a model can look before it commits.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `workflow_id` | string | yes |

**Result**

```json
{
  "ready": false,
  "checks": [
    { "check": "instance_reachable",   "status": "pass" },
    { "check": "workflow_exists",      "status": "pass" },
    { "check": "workflow_active",      "status": "pass" },
    { "check": "definition_unchanged", "status": "fail",
      "code": "DEFINITION_DRIFT",
      "detail": { "registered": "sha256:1a2b…", "live": "sha256:9f8e…" } },
    { "check": "credential_bindings",  "status": "skipped",
      "detail": "Not evaluated after a prior failure." },
    { "check": "credential_validity",  "status": "unverifiable",
      "code": "CREDENTIAL_VALIDITY_UNVERIFIED",
      "detail": "Operator verifies that credentials are bound, not that they work." },
    { "check": "correlation",          "status": "warn",
      "code": "NO_EXECUTION_CORRELATION",
      "detail": "This workflow returns no execution ID. Reconciliation after an indeterminate dispatch will be manual." }
  ],
  "checked_at": "2026-08-25T14:03:11Z"
}
```

**Statuses.** `pass`, `fail`, `skipped`, and two non-blocking statuses introduced by
[ADR-009](adr/ADR-009-dispatch-correlation.md): `warn` (a real capability limitation) and
`unverifiable` (a condition Operator has no supported mechanism to test). **Only `fail`
sets `ready: false` and only `fail` produces `BLOCKED`.**

Check codes: `INSTANCE_UNREACHABLE`, `WORKFLOW_MISSING_ON_INSTANCE`, `WORKFLOW_INACTIVE`,
`DEFINITION_DRIFT`, `MISSING_NODE_CREDENTIALS`, `CREDENTIAL_VALIDITY_UNVERIFIED`,
`NO_EXECUTION_CORRELATION`, `UNATTENDED_EXECUTION`.

`MISSING_NODE_CREDENTIALS` reports that a node has no credential **bound**. It is not a
statement that a bound credential is valid — Operator never makes that claim without a
supported n8n mechanism that tests it, and reports `CREDENTIAL_VALIDITY_UNVERIFIED` instead.
A bound-but-expired credential passes preflight and fails at execution.

`UNATTENDED_EXECUTION` is a `warn` emitted for a workflow eligible for T05
(`side_effects: read_only` **and** `approval: none`): it will run with no human in the loop,
on the strength of the registry's own classification.

**Errors:** `WORKFLOW_NOT_FOUND`.

---

### 2.6 `prepare_operation`

Validate, preflight, and mint an operation handle. Creates an operation record but runs
nothing. This is the only way to obtain the authority to execute
([ADR-003](adr/ADR-003-operation-handles.md)).

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `workflow_id` | string | yes | Registry ID. |
| `arguments` | object | yes | Validated against the workflow's input schema. |
| `idempotency_key` | string | no | Client-supplied. Replaying the same key with the same arguments returns the same operation. |
| `reason` | string | no | Free text shown to the human approver. Advisory only; never affects policy ([ADR-007](adr/ADR-007-deterministic-before-llm.md)). |

**Result — approval required, local caller** (stdio, or Streamable HTTP on loopback)

```json
{
  "operation_id": "op_01JQ…",
  "state": "PENDING_APPROVAL",
  "workflow_id": "crm.sync_contact",
  "approval_required": true,
  "approval_instructions": "A human must approve this operation on the Operator machine: run `n8n-operator operations approve op_01JQ…`. You cannot approve it yourself.",
  "approval_url": "http://127.0.0.1:8765/approve/6f3c…",
  "approval_expires_at": "2026-08-25T14:18:11Z",
  "created_at": "2026-08-25T14:03:11Z",
  "idempotent_replay": false
}
```

**Result — approval required, remote caller** (Streamable HTTP on a non-loopback bind)

```json
{
  "operation_id": "op_01JQ…",
  "state": "PENDING_APPROVAL",
  "workflow_id": "crm.sync_contact",
  "approval_required": true,
  "approval_instructions": "A human must approve this operation on the Operator machine: run `n8n-operator operations approve op_01JQ…`. You cannot approve it yourself.",
  "approval_expires_at": "2026-08-25T14:18:11Z",
  "created_at": "2026-08-25T14:03:11Z",
  "idempotent_replay": false
}
```

`approval_url` is **absent** for a remote caller — a loopback address means nothing on the
caller's machine, and returning one invites a model to report that it "sent the approval
link" while the operation quietly expires (invariant I12, boundary B13, threat T-38).
`approval_required` is the field to branch on; the URL is never the signal.

**Result — auto-approved** (T05: `approval: none` **and** `side_effects: read_only`, rule R5)

```json
{
  "operation_id": "op_01JQ…",
  "state": "APPROVED",
  "workflow_id": "reports.pipeline_summary",
  "approval_required": false,
  "execution_deadline": "2026-08-25T14:08:11Z",
  "created_at": "2026-08-25T14:03:11Z",
  "idempotent_replay": false
}
```

**Result — validation failed**

```json
{
  "operation_id": "op_01JQ…",
  "state": "INVALID",
  "workflow_id": "crm.sync_contact",
  "errors": [ { "path": "/email", "code": "REQUIRED", "message": "…" } ]
}
```

**Result — preflight failed**

```json
{
  "operation_id": "op_01JQ…",
  "state": "BLOCKED",
  "workflow_id": "crm.sync_contact",
  "checks": [ { "check": "workflow_active", "status": "fail", "code": "WORKFLOW_INACTIVE" } ]
}
```

`INVALID` and `BLOCKED` are **results, not errors** — the call succeeded and produced a
governed, audited outcome. Only failures to *interpret the request at all*
(`WORKFLOW_NOT_FOUND`, `IDEMPOTENCY_CONFLICT`) and refusals to record it
(`ARGUMENTS_TOO_LARGE`) are errors.

`ARGUMENTS_TOO_LARGE` is deliberately an error rather than an `INVALID` operation: recording
the request is the thing being refused, so no operation row is written
([ADR-011](adr/ADR-011-argument-limits-and-idempotency.md), invariant I10). The limit is
enforced in the core over the canonical serialization, so it is identical over stdio,
Streamable HTTP, and the CLI (boundary B12).

**Idempotency namespace.** A key is scoped to
`(principal, environment, workflow_id, idempotency_key)`. The same key under a different
workflow is a different namespace and yields an independent operation; the same namespace
and key with different arguments is `IDEMPOTENCY_CONFLICT`.

The approval URL, where present, is a convenience for the human, not a capability for the
model: the client cannot approve by fetching it (boundary B4).

**Errors:** `WORKFLOW_NOT_FOUND`, `WORKFLOW_DISABLED`, `IDEMPOTENCY_CONFLICT`,
`ARGUMENTS_TOO_LARGE`, `RATE_LIMITED`, `CONCURRENCY_LIMIT_REACHED`, `REGISTRY_UNAVAILABLE`.

---

### 2.7 `get_operation`

Current state of one operation. The intended polling tool while awaiting approval.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `operation_id` | string | yes |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "workflow_id": "crm.sync_contact",
  "state": "APPROVED",
  "created_at": "2026-08-25T14:03:11Z",
  "state_changed_at": "2026-08-25T14:05:02Z",
  "approval_expires_at": null,
  "execution_deadline": "2026-08-25T14:10:02Z",
  "approval": { "required": true, "decided": true, "decision": "approved",
                "decided_at": "2026-08-25T14:05:02Z" },
  "handle_used": false,
  "arguments": { "email": "[REDACTED]", "tier": "pro" }
}
```

Arguments are echoed **post-redaction**. An expired operation reads as `EXPIRED` even if no
sweeper has ever run: this call applies any overdue T08 or T11 transition in the same
transaction before evaluating state (invariant I9, ARCHITECTURE section 8).

**Errors:** `OPERATION_NOT_FOUND`.

---

### 2.8 `execute_operation`

Burn the handle and dispatch to n8n. **The only tool in the product that causes an
external side effect.**

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `operation_id` | string | yes | Must be in `APPROVED`. |
| `handle` | string | yes | The handle returned by `prepare_operation`. Single-use. |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "state": "SUCCEEDED",
  "started_at": "2026-08-25T14:05:40Z",
  "finished_at": "2026-08-25T14:05:42Z",
  "duration_ms": 2104,
  "result": { "contact_id": "c_8891", "created": false, "truncated": false }
}
```

**Result — indeterminate**

```json
{
  "operation_id": "op_01JQ…",
  "state": "UNKNOWN",
  "code": "DISPATCH_INDETERMINATE",
  "message": "The request was sent but the outcome could not be confirmed. It may or may not have taken effect. Do not retry: verify the downstream system, then prepare a new operation if needed.",
  "started_at": "2026-08-25T14:05:40Z",
  "correlation": { "available": false, "reason": "NO_EXECUTION_CORRELATION" }
}
```

`UNKNOWN` is a deliberate, terminal, human-resolved state. Nothing in v1 automatically
retries it, and no transition leads out of it ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)).
The message is written to tell a model plainly not to retry, because the model's
instinct will be to retry.

**Operator never infers that a timed-out dispatch did not run**
([ADR-009](adr/ADR-009-dispatch-correlation.md)). A timeout means no response arrived inside
the window — not that nothing happened. There is no error-class check or elapsed-time rule
that turns this into `FAILED`.

The `correlation` block says whether an n8n execution ID is available for reconciliation.
For a workflow registered with `trigger.correlation: response_envelope` that returned one,
it reads `{"available": true, "execution_id": "1042"}`. `execution_id` is an n8n *execution*
identifier, not a workflow identifier, and does not breach boundary B5.

**Errors:** `OPERATION_NOT_FOUND`, `APPROVAL_REQUIRED`, `OPERATION_EXPIRED`,
`OPERATION_CANCELED`, `HANDLE_INVALID`, `HANDLE_ALREADY_USED`, `ARGUMENT_MISMATCH`,
`DEFINITION_DRIFT`, `CONCURRENCY_LIMIT_REACHED`, `INSTANCE_UNREACHABLE`.

`DEFINITION_DRIFT` here means the workflow changed *between approval and execution*.
The approval is void; nothing was dispatched (AC-13).

---

### 2.9 `cancel_operation`

Terminate a `PENDING_APPROVAL` or `APPROVED` operation before it runs.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `operation_id` | string | yes |
| `reason` | string | no |

**Result:** `{ "operation_id": "op_01JQ…", "state": "CANCELED", "canceled_at": "…" }`

**Errors:** `OPERATION_NOT_FOUND`, `INVALID_STATE_TRANSITION` (already terminal or
already `EXECUTING`).

---

### 2.10 `list_operations`

Filterable history. The model's memory of what it has already done — the first defense
against duplicate work.

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `workflow_id` | string | no | |
| `state` | array of string | no | Any of the twelve states in BUILD_PLAN section 5.1. |
| `since` | string | no | RFC 3339. |
| `limit` | integer | no | 1–100, default 20. |
| `cursor` | string | no | Opaque pagination cursor. |

**Result**

```json
{
  "operations": [
    { "operation_id": "op_01JQ…", "workflow_id": "crm.sync_contact",
      "state": "SUCCEEDED", "created_at": "…", "state_changed_at": "…" }
  ],
  "next_cursor": null
}
```

**Errors:** `INVALID_ARGUMENTS`.

---

### 2.11 `get_execution_result`

The redacted, size-capped result of a completed operation.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `operation_id` | string | yes |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "state": "SUCCEEDED",
  "status": "success",
  "started_at": "…", "finished_at": "…",
  "result": { "contact_id": "c_8891" },
  "truncated": false
}
```

For a `FAILED` operation, `result` is absent and `error` is present:

```json
{ "error": { "node": "HTTP Request", "type": "NodeApiError",
             "message": "Request failed with status 422" } }
```

**Errors:** `OPERATION_NOT_FOUND`, `RESULT_NOT_AVAILABLE` (the operation never executed).

---

### 2.12 `get_execution_log`

A redacted structural trace for debugging. Node names, order, per-node status, and the
failure point — enough to diagnose, not enough to exfiltrate.

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `operation_id` | string | yes | |
| `include_node_data` | boolean | no | Honored only if the registry entry sets `output.include_node_trace: true`. Otherwise silently omitted; requesting it is never an error. |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "state": "FAILED",
  "nodes": [
    { "name": "Webhook",      "type": "n8n-nodes-base.webhook",     "status": "success", "duration_ms": 3 },
    { "name": "Set Fields",   "type": "n8n-nodes-base.set",         "status": "success", "duration_ms": 1 },
    { "name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "status": "error",
      "duration_ms": 812,
      "error": { "type": "NodeApiError", "message": "Request failed with status 422",
                 "http_status": 422 } }
  ],
  "failed_node": "HTTP Request",
  "truncated": false
}
```

**Errors:** `OPERATION_NOT_FOUND`, `RESULT_NOT_AVAILABLE`.

---

## 3. v1 resources

| URI | Content |
|---|---|
| `registry://workflows` | The active registry snapshot as the model sees it: registry IDs, titles, descriptions, schemas, risk and side-effect classes. Excludes `n8n_workflow_id`, `trigger`, and every `secret_ref`. |
| `audit://operations/{operation_id}` | The ordered event chain for one operation: transitions, actors, timestamps, redacted details. |

No prompts are exposed in v1 (BUILD_PLAN section 7.1).

---

## 4. Error taxonomy

Normative. Implemented once in `errors.py`; adapters map these to MCP tool errors, CLI
exit codes, or HTTP status without inventing new codes.

> **Superseded spelling.** Phase 0 spelled the idempotency error
> `IDEMPOTENCY_KEY_CONFLICT`. The normative code is `IDEMPOTENCY_CONFLICT` — the conflict is
> between requests within a namespace, not between keys
> ([ADR-011](adr/ADR-011-argument-limits-and-idempotency.md)). The old spelling must not
> appear in code, tests, or documentation; check D11 enforces its absence.

| Code | Meaning | Model's correct next move |
|---|---|---|
| `WORKFLOW_NOT_FOUND` | No such registry ID (also returned for workflows that exist on n8n but are unregistered). | Call `list_workflows`. |
| `WORKFLOW_DISABLED` | Registered but `enabled: false`. | Ask the operator; do not retry. |
| `INVALID_ARGUMENTS` | Tool arguments failed the tool's own schema. | Fix the call shape. |
| `IDEMPOTENCY_CONFLICT` | Key reused **within the same namespace** (principal, environment, workflow) with different arguments. | Use a new key, or reuse the original arguments. |
| `ARGUMENTS_TOO_LARGE` | Canonical argument size exceeds the effective limit. No operation was created. | Send less data; the limit is reported in `details`. Do not split a side-effecting call to evade it. |
| `OPERATION_NOT_FOUND` | Unknown operation ID. | Call `list_operations`. |
| `APPROVAL_REQUIRED` | Execute attempted while `PENDING_APPROVAL`. | Wait; poll `get_operation`. Do not retry in a tight loop. |
| `OPERATION_EXPIRED` | Approval or execution window elapsed. | Prepare a new operation. |
| `OPERATION_CANCELED` | Operation was canceled. | Prepare a new one if still wanted. |
| `INVALID_STATE_TRANSITION` | Requested move is not an edge in BUILD_PLAN section 5.2. | Read `get_operation` and act on actual state. |
| `HANDLE_INVALID` | Handle does not match the operation or principal. | Re-prepare. |
| `HANDLE_ALREADY_USED` | Handle already burned. | **Do not retry.** Check `get_operation`; the run may have happened. |
| `ARGUMENT_MISMATCH` | Fingerprint at execute differs from prepare. | Re-prepare with the intended arguments. |
| `DEFINITION_DRIFT` | Live definition differs from the registered hash. | Stop. This needs an operator, not a retry. |
| `WORKFLOW_INACTIVE` | Workflow is deactivated in n8n. | Ask the operator. |
| `WORKFLOW_MISSING_ON_INSTANCE` | Registered, but absent from n8n. | Ask the operator. |
| `MISSING_NODE_CREDENTIALS` | A node has no credential **bound** on the instance. Says nothing about whether a bound credential is valid. | Ask the operator. |
| `INSTANCE_UNREACHABLE` | n8n did not respond. | Retry later at human pace; do not loop. |
| `DISPATCH_INDETERMINATE` | Sent, outcome unconfirmed; operation is `UNKNOWN`. | **Never retry.** Verify downstream, then decide. |
| `RATE_LIMITED` | Registry rate limit exceeded. | Back off; the limit is per-workflow. |
| `CONCURRENCY_LIMIT_REACHED` | `max_concurrent` reached for this workflow. | Wait for the in-flight operation. |
| `RESULT_NOT_AVAILABLE` | Operation never executed. | Check state first. |
| `REGISTRY_UNAVAILABLE` | Registry failed to load. | Operator action required; the server should not be serving. |
| `INTERNAL_ERROR` | Unexpected server fault. | Report; do not retry blindly. |

### 4.1 Error shape

```json
{
  "code": "HANDLE_ALREADY_USED",
  "message": "This operation handle has already been used. The workflow may have already run.",
  "details": { "operation_id": "op_01JQ…", "burned_at": "2026-08-25T14:05:42Z" },
  "retryable": false
}
```

`retryable` is advisory guidance for the model, not a promise the server will behave
differently. It is `false` for every side-effect-adjacent failure — most importantly
`HANDLE_ALREADY_USED` and `DISPATCH_INDETERMINATE`.

---

## 5. v2 tools (contracts to be specified in the v2 phase)

Inventory is normative in BUILD_PLAN section 7.2: `whoami`, `list_environments`,
`request_approval`, `get_approval_status`, `retry_operation`,
`diff_workflow_definition`, `get_metrics`, `list_audit_events`.

Contract changes to v1 tools in v2:

- Every tool gains optional `environment`, defaulting to the caller's default environment.
- Every result gains `environment`.
- Results are filtered by the caller's RBAC scope. An unauthorized workflow returns
  `WORKFLOW_NOT_FOUND`, never `FORBIDDEN` — authorization must not be an enumeration
  oracle.
- `retry_operation` returns a **new** `operation_id` with `parent_operation_id` set. It
  never revives the original, and it never reuses the original's approval: validation,
  preflight, and approval are all recalculated against the snapshot in force at retry time
  ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md),
  [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md), invariant I11). A
  `read_only` retry reaching `APPROVED` via T05 is recalculation, not reuse.

---

## 6. v3 tools (contracts to be specified in the v3 phase)

Inventory is normative in BUILD_PLAN section 7.3. Two constraints already fixed:

- `apply_workflow_change` is gated exactly like `execute_operation`: a plan handle from
  `plan_workflow_change`, single-use, human-approved, drift-checked at apply time.
- `suggest_remediation` and `instantiate_template` are **pure**. They return proposals
  and source text. Nothing they return takes effect without a separate governed
  operation.
