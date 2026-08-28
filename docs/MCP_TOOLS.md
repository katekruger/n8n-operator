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
    { "check": "instance_reachable",    "status": "pass" },
    { "check": "compatible_version",    "status": "pass" },
    { "check": "workflow_exists",       "status": "pass" },
    { "check": "workflow_active",       "status": "pass" },
    { "check": "trigger_compatibility", "status": "pass" },
    { "check": "definition_unchanged",  "status": "fail",
      "code": "DEFINITION_DRIFT",
      "detail": { "registered": "sha256:1a2b…", "live": "sha256:9f8e…" } },
    { "check": "credential_bindings",   "status": "skipped",
      "detail": "Not evaluated after a prior failure." },
    { "check": "credential_validity",   "status": "unverifiable",
      "code": "CREDENTIAL_VALIDITY_UNVERIFIED",
      "detail": "Operator verifies that credentials are bound, not that they work." },
    { "check": "correlation",           "status": "warn",
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

Check codes: `INSTANCE_UNREACHABLE`, `API_VERSION_UNVERIFIED`, `WORKFLOW_MISSING_ON_INSTANCE`,
`WORKFLOW_INACTIVE`, `TRIGGER_INCOMPATIBLE`, `DEFINITION_DRIFT`, `MISSING_NODE_CREDENTIALS`,
`CREDENTIAL_VALIDITY_UNVERIFIED`, `NO_EXECUTION_CORRELATION`, `UNATTENDED_EXECUTION`.

`compatible_version` compares the n8n public API's own spec version (there is no endpoint
that returns the n8n release version itself — see
[N8N_COMPATIBILITY.md](N8N_COMPATIBILITY.md) section 10) against a configured supported
set. No configured set, or a version that could not be determined, is `unverifiable`; a
determined version outside the configured set is `warn`, never `fail` — it is not a
precise enough signal to block on.

`trigger_compatibility` compares the registry's `trigger.path`/`trigger.method` against
the live workflow's own trigger node configuration, independent of the definition-hash
check — a mismatch here points a caller directly at "the trigger changed" rather than an
opaque hash difference.

`MISSING_NODE_CREDENTIALS` reports that a node has no credential **bound**. It is not a
statement that a bound credential is valid — Operator never makes that claim without a
supported n8n mechanism that tests it, and reports `CREDENTIAL_VALIDITY_UNVERIFIED` instead.
A bound-but-expired credential passes preflight and fails at execution. n8n exposes a
credential-test endpoint (`POST /credentials/{id}/test`); Operator does not call it — tried
against a live instance, it proved unreliable even for a common credential type (see
N8N_COMPATIBILITY.md section 9), which is direct evidence for staying with `unverifiable`
rather than a reason to build around the unreliability.

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
| `ENVIRONMENT_NOT_FOUND` | *(v2)* No such environment ID visible to this caller — identical for a nonexistent ID and one the caller is not authorized to see (no enumeration oracle, [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)). | Call `list_environments`. |
| `ENVIRONMENT_REQUIRED` | *(v2)* The resolved organization has more than one environment and none was named — never defaulted, even when only one is production ([ADR-016](adr/ADR-016-environment-registry-overlays.md)). | Call `list_environments`; name one explicitly. |
| `ENVIRONMENT_ARCHIVED` | *(v2)* A state-changing call named an archived environment. Read tools still resolve it. | Ask the operator; use a live environment for new work. |
| `APPROVER_NOT_IN_POLICY` | *(v2)* `request_approval`'s `approvers` named a principal outside the operation's approval-policy snapshot. | Omit `approvers`, or check `get_approval_status` for the real snapshot. |
| `RETRY_NOT_APPLICABLE` | *(v2)* `retry_operation`'s parent is not in a state representing "did not run as intended" (`SUCCEEDED`, `CANCELED`, `INVALID`, `EXECUTING`, `PENDING_APPROVAL`, or `APPROVED`). | Do not retry; if a genuinely new run is wanted, call `prepare_operation`. |

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

## 5. v2 tools

> Complete contracts, specified at v2 stage 00 (contract closure) alongside
> [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md) through
> [ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md). Inventory is normative in
> BUILD_PLAN section 7.2. Conventions in section 1 apply unchanged; the additions below
> are cumulative, not replacements.

**Organization resolution.** v2 has no MCP tool that selects "the active organization."
Every tool below except `whoami` and `list_environments` resolves its organization
*through* the `environment` argument (each environment belongs to exactly one
organization, [ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md) section
2) — naming an environment names an organization. `whoami` and `list_environments`
operate across every organization the caller belongs to, precisely so a caller can
discover what to name before naming anything.

**Environment default resolution** ([ADR-016](adr/ADR-016-environment-registry-overlays.md)
section 3): omitting `environment` is valid only when the resolved organization has
exactly one environment — that environment is used, whether or not it is
`is_production`. The instant a second environment exists for that organization,
omitting `environment` is `ENVIRONMENT_REQUIRED`, even if only one of the environments
is production. Naming an environment ID outside the caller's authorized organizations,
or one that does not exist, is `ENVIRONMENT_NOT_FOUND` — identical wording either way
(no enumeration oracle, [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)
section 3). Naming an archived environment for a state-changing call
(`prepare_operation`) is `ENVIRONMENT_ARCHIVED`; read tools resolve an archived
environment normally, because historical operations must stay readable
([ADR-016](adr/ADR-016-environment-registry-overlays.md) section 4).

**Authorization filtering.** Every result in v2 is filtered to what
[ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)'s role-capability ∧
workflow-scope ∧ environment-scope evaluation authorizes for the caller, applied
*before* any list is built or any aggregate computed — never as a post-hoc redaction.
An unauthorized workflow or environment produces `WORKFLOW_NOT_FOUND` /
`ENVIRONMENT_NOT_FOUND`, the same as a nonexistent one. **There is no `FORBIDDEN` error
code anywhere in v2.**

---

### 5.1 `whoami`

Resolved identity: who the caller is, and every organization, role set, and
environment they can see. The one tool a caller needs before naming anything else.

**Arguments:** none.

**Result**

```json
{
  "principal_id": "prin_01JQ…",
  "kind": "user",
  "display_name": "Carolyn Stumph",
  "organizations": [
    {
      "organization_id": "org_01JQ…",
      "name": "Acme GTM",
      "roles": ["operator", "approver"],
      "environments": [
        { "environment_id": "env_01JQ…", "name": "staging", "is_production": false },
        { "environment_id": "env_01JR…", "name": "prod", "is_production": true }
      ]
    }
  ]
}
```

A caller who is a member of no organization gets `"organizations": []` — a normal,
expected result for a freshly authenticated principal before an admin grants any
membership ([ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md)
section 3), never an error.

**Errors:** none.

---

### 5.2 `list_environments`

Every environment the caller can see, across every organization they belong to.

**Arguments:** none.

**Result**

```json
{
  "environments": [
    {
      "environment_id": "env_01JQ…",
      "organization_id": "org_01JQ…",
      "name": "staging",
      "is_production": false,
      "archived": false,
      "approval_policy_summary": "auto-approve read_only; 1 approver otherwise"
    },
    {
      "environment_id": "env_01JR…",
      "organization_id": "org_01JQ…",
      "name": "prod",
      "is_production": true,
      "archived": false,
      "approval_policy_summary": "2-of-3 approvers required, always"
    }
  ]
}
```

Archived environments ([ADR-016](adr/ADR-016-environment-registry-overlays.md)
section 4) appear only for callers holding `admin` in that environment's organization,
with `"archived": true`.

**Errors:** none.

---

### 5.3 `request_approval`

Route a `PENDING_APPROVAL` operation's approval to its eligible approvers and (re)send
notifications. **Still cannot grant approval** — the out-of-band decision itself
crosses only the CLI or the approval app, exactly as in v1 (boundary B4).

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `operation_id` | string | yes | Must be `PENDING_APPROVAL`. |
| `approvers` | array of string | no | Principal IDs to notify. Must be a subset of the operation's approval-policy snapshot ([ADR-017](adr/ADR-017-team-approval-quorum-semantics.md) section 1) — never a way to add someone outside it. Omitted: notifies the full snapshot. |
| `message` | string | no | Advisory, shown alongside the notification. Never affects policy ([ADR-007](adr/ADR-007-deterministic-before-llm.md)). |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "quorum_count": 2,
  "approval_policy_snapshot": ["prin_01JA…", "prin_01JB…", "prin_01JC…"],
  "notified": ["prin_01JA…", "prin_01JB…"],
  "state": "PENDING_APPROVAL"
}
```

Calling this more than once re-sends notifications; delivery is deduplicated by
`(operation_id, principal_id, event_type)` so a caller invoking it twice does not
double-notify anyone ([ADR-018](adr/ADR-018-notification-and-alert-hook-delivery.md)
section 2).

**Errors:** `OPERATION_NOT_FOUND`, `INVALID_STATE_TRANSITION` (operation is not
`PENDING_APPROVAL`), `APPROVER_NOT_IN_POLICY` (a named `approvers` entry is not in the
operation's snapshot).

---

### 5.4 `get_approval_status`

Which approvals have been collected, which are outstanding, against the required
quorum.

**Arguments**

| Field | Type | Required |
|---|---|---|
| `operation_id` | string | yes |

**Result**

```json
{
  "operation_id": "op_01JQ…",
  "quorum_count": 2,
  "approval_policy_snapshot": ["prin_01JA…", "prin_01JB…", "prin_01JC…"],
  "decisions": [
    { "principal_id": "prin_01JA…", "decision": "approved", "decided_at": "2026-08-28T14:05:02Z" }
  ],
  "outstanding": ["prin_01JB…", "prin_01JC…"],
  "ready": false
}
```

`ready: true` means quorum is reached with zero rejections and the operation has moved
or will move to `APPROVED` (T06); a single rejection anywhere in `decisions` means the
operation is already `REJECTED` and `outstanding` is emptied, not because those
approvers decided but because a decision is no longer possible
([ADR-017](adr/ADR-017-team-approval-quorum-semantics.md) section 2).

**Errors:** `OPERATION_NOT_FOUND`.

---

### 5.5 `retry_operation`

Governed retry: mint a **new** operation linked to a terminal parent that did not
reach its intended outcome, with validation, preflight, and approval all recalculated
from scratch against the snapshot in force *now* ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md),
[ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md), invariant I11).

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `operation_id` | string | yes | The parent. Must be `FAILED`, `UNKNOWN`, `BLOCKED`, `EXPIRED`, or `REJECTED` — the states representing "did not run as intended." |
| `idempotency_key` | string | no | Same semantics as `prepare_operation` (invariant I8), scoped additionally to the parent. |
| `reason` | string | no | Advisory, shown to the human approver if one is needed. Never affects policy. |

**Result:** identical shape to `prepare_operation`'s four result variants
(`PENDING_APPROVAL` / `APPROVED` / `INVALID` / `BLOCKED`), plus `parent_operation_id`
in every variant.

```json
{
  "operation_id": "op_01JZ…",
  "parent_operation_id": "op_01JQ…",
  "state": "PENDING_APPROVAL",
  "workflow_id": "crm.sync_contact",
  "approval_required": true,
  "approval_instructions": "…",
  "approval_expires_at": "2026-08-28T15:00:00Z",
  "created_at": "2026-08-28T14:45:00Z",
  "idempotent_replay": false
}
```

**Never returned:** `SUCCEEDED` directly from this tool — a retry that would succeed
still has to actually execute via `execute_operation` after reaching `APPROVED`, exactly
like any other operation. `retry_operation` never dispatches to n8n itself.

The parent is never moved, never re-read as anything but what it already was, and its
handle stays burned. A `read_only`/`approval: none` parent retried under an unchanged
registry snapshot reaches `APPROVED` via T05 exactly as a first attempt would — this is
recalculation reaching the same conclusion, not approval reuse
([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) section 1).

**Errors:** `OPERATION_NOT_FOUND`, `RETRY_NOT_APPLICABLE` (parent is SUCCEEDED,
CANCELED, INVALID, EXECUTING, PENDING_APPROVAL, or APPROVED — never a state
representing "did not run as intended"), `IDEMPOTENCY_CONFLICT`,
`ARGUMENTS_TOO_LARGE`, `WORKFLOW_DISABLED`, `REGISTRY_UNAVAILABLE`.

---

### 5.6 `diff_workflow_definition`

Structural diff between the registered `definition_hash` and the live n8n definition —
turning drift from an opaque hash mismatch into a reviewable change list.

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `workflow_id` | string | yes | |
| `environment` | string | no | Standard resolution above. |

**Result**

```json
{
  "workflow_id": "crm.sync_contact",
  "environment": "prod",
  "registered_hash": "sha256:1a2b…",
  "live_hash": "sha256:9f8e…",
  "changed": true,
  "diff": [
    { "path": "/nodes/2/parameters/url", "change_type": "modified",
      "registered_value": "[REDACTED]", "live_value": "[REDACTED]" },
    { "path": "/nodes/5", "change_type": "added" }
  ]
}
```

The diff is computed over the same canonical form `definition_hash` is taken over
([ADR-008](adr/ADR-008-conservative-definition-canonicalization.md)) — a field on the
canonicalization exclusion allowlist never appears as a diff entry, because it never
contributed to either hash. Values matched by the workflow's own `output.redact` paths
are redacted in the diff exactly as in any other tool result (boundary B6); an entry
whose path is not otherwise redact-configured still shows real values, since a
definition diff (unlike an execution result) contains no downstream PII by
construction — only node configuration the operator already authored.

**Errors:** `WORKFLOW_NOT_FOUND`, `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED`,
`INSTANCE_UNREACHABLE`.

---

### 5.7 `get_metrics`

Operation counts, outcome distribution, and latency percentiles, bounded and
authorization-filtered before aggregation
([ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md)).

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `environment` | string | no | Standard resolution above. |
| `window` | enum | no | `1h` / `24h` / `7d` / `30d`. Default `24h`. No arbitrary custom range. |
| `group_by` | enum | no | `workflow` / `risk` / `side_effects` / `outcome`. Omitted: totals only. |

**Result**

```json
{
  "environment": "prod",
  "window": "24h",
  "generated_at": "2026-08-28T18:00:00Z",
  "totals": {
    "count": 214,
    "by_outcome": { "succeeded": 190, "failed": 12, "unknown": 2, "blocked": 10 }
  },
  "latency_ms": { "p50": 812, "p95": 2104, "p99": null, "p99_reason": "insufficient_sample" },
  "breakdown": [
    { "key": "crm.sync_contact", "count": 140, "by_outcome": { "succeeded": 138, "failed": 2 } },
    { "key": "other", "count": 74, "note": "51 additional workflows below the top-50 cutoff" }
  ]
}
```

A percentile is `null` with a `"_reason": "insufficient_sample"` field whenever its
bucket has fewer than 10 samples in the window — never a number computed from too few
executions to mean anything statistically, and never one precise enough to identify a
single operation's exact duration ([ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md)
section 4). A `breakdown` carries at most 50 distinct entries; beyond that, a single
`"other"` entry with an aggregate count only — no further identifiers.

**Errors:** `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED`.

---

### 5.8 `list_audit_events`

Query the audit chain within the caller's authorization scope.

**Arguments**

| Field | Type | Required | Notes |
|---|---|---|---|
| `environment` | string | no | Standard resolution above. |
| `workflow_id` | string | no | Filter to one workflow. |
| `since` | string | no | RFC 3339. |
| `limit` | integer | no | 1–100, default 20. |
| `cursor` | string | no | Opaque pagination cursor, anchored to `audit_log.seq` — no offset paging over an append-only log ([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md) section 3). |

**Result**

```json
{
  "events": [
    { "seq": 40231, "occurred_at": "2026-08-28T14:05:02Z", "actor": "prin_01JA…",
      "action": "approval.granted", "subject_type": "operation", "subject_id": "op_01JQ…",
      "outcome": "allowed", "detail": {} }
  ],
  "next_cursor": "eyJzZXEiOjQwMjMxfQ"
}
```

Authorization filters the query before pagination runs: an event whose `subject_id`
resolves to a workflow or environment outside the caller's scope is excluded entirely,
never returned with a redacted `detail` ([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md)
section 3, [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)). `detail` carries
the same write-time redaction v1 already applies (BUILD_PLAN section 8.1); there is no
broader-role view of any entry's raw content.

**Errors:** `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED`, `INVALID_ARGUMENTS`.

---

### 5.9 Contract changes to every v1 tool in v2

Every v1 tool keeps its v1 argument, result, and error shape exactly, plus the
additions below — nothing in section 2 is removed or narrowed.

| Tool | `environment` | New/changed result field | Pagination | Authorization filtering | New errors |
|---|---|---|---|---|---|
| `list_workflows` | optional, standard resolution | `environment` added to the result envelope | **New in v2**: `cursor`/`limit` (1–100, default 20), same shape as v1 `list_operations` | Excludes workflows outside workflow-scope | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `describe_workflow` | optional, standard resolution | `environment` added | N/A (single resource) | Unauthorized workflow → `WORKFLOW_NOT_FOUND` | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `get_instance_health` | optional, standard resolution | `environment` added | N/A | Unauthorized environment → `ENVIRONMENT_NOT_FOUND` | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `validate_input` | optional, standard resolution | `environment` added | N/A | Unauthorized workflow → `WORKFLOW_NOT_FOUND` | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `preflight_workflow` | optional, standard resolution | `environment` added | N/A | Unauthorized workflow → `WORKFLOW_NOT_FOUND` | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `prepare_operation` | optional, standard resolution | `environment` added; approval-required results additionally include `approval_policy_snapshot` size (count only) when quorum > 1 | N/A | Unauthorized workflow/environment → `WORKFLOW_NOT_FOUND`/`ENVIRONMENT_NOT_FOUND`; not `operator` for this workflow+environment → same | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED`, `ENVIRONMENT_ARCHIVED` |
| `get_operation` | *(implicit — carried on the operation itself)* | `environment` added | N/A | Operation's own environment outside caller's scope → `OPERATION_NOT_FOUND` | none new |
| `execute_operation` | *(implicit)* | `environment` added | N/A | Same as `get_operation` | none new |
| `cancel_operation` | *(implicit)* | `environment` added | N/A | Same as `get_operation` | none new |
| `list_operations` | optional, standard resolution | `environment` added per row; `environment` filter argument added | Unchanged shape, now environment-scoped | Excludes operations outside workflow/environment scope | `ENVIRONMENT_NOT_FOUND`, `ENVIRONMENT_REQUIRED` |
| `get_execution_result` | *(implicit)* | `environment` added | N/A | Same as `get_operation` | none new |
| `get_execution_log` | *(implicit)* | `environment` added | N/A | Same as `get_operation` | none new |

"Implicit" means the tool's only argument is an `operation_id` that already resolved
to one environment at `prepare_operation` time — there is nothing to disambiguate, and
adding a redundant `environment` argument here would only create a way for it to
disagree with the operation's real one. Authorization for these six tools is instead:
does the caller's scope cover the operation's *actual* environment and workflow, with
an unauthorized operation returning `OPERATION_NOT_FOUND` — the same anti-enumeration
answer as everywhere else, applied to operations instead of workflows or environments.

---

## 6. v3 tools (contracts to be specified in the v3 phase)

Inventory is normative in BUILD_PLAN section 7.3. Two constraints already fixed:

- `apply_workflow_change` is gated exactly like `execute_operation`: a plan handle from
  `plan_workflow_change`, single-use, human-approved, drift-checked at apply time.
- `suggest_remediation` and `instantiate_template` are **pure**. They return proposals
  and source text. Nothing they return takes effect without a separate governed
  operation.
