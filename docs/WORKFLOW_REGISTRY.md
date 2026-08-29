# n8n Operator — Workflow Registry Reference

> The registry is the allowlist. A workflow that is not in it does not exist to any MCP
> client, however live it is on the n8n instance ([ADR-002](adr/ADR-002-default-deny-registry.md)).
>
> The **normative schema** is [BUILD_PLAN.md](BUILD_PLAN.md) section 6 — field names,
> types, defaults, load-time rules R1–R12, and the canonicalization rules CAN-01–CAN-07
> live there. This document is the authoring guide: how to register a workflow, how to
> choose the classifications that drive policy, and how to operate the registry over time.

---

## 1. What registering a workflow means

Adding an entry is a security decision with four parts. Writing them down is the point
of the exercise.

1. **This workflow may be invoked by an agent.** You are handing a model a button.
2. **Only these arguments are valid.** The `input_schema` is the complete contract.
   Anything outside it is rejected before n8n is touched.
3. **This is what it does to the world.** `side_effects` and `risk` drive whether a
   human must approve each invocation.
4. **This exact definition is what I reviewed.** `definition_hash` pins the node graph
   you inspected. If it changes, every operation is blocked until you re-review.

If you cannot state all four confidently, the workflow is not ready to register.

---

## 2. A minimal registry

```yaml
apiVersion: n8n-operator/v1

metadata:
  name: carolyn-personal
  description: Workflows exposed to MCP clients from my personal n8n instance.

defaults:
  approval: required
  timeout_seconds: 60
  approval_ttl_seconds: 900
  execution_ttl_seconds: 300

workflows:
  - id: reports.pipeline_summary
    n8n_workflow_id: "7Qx4kLmN2pRstUvW"
    title: Summarize the current sales pipeline
    description: >
      Returns aggregate pipeline figures by stage for a date range. Reads only;
      writes nothing anywhere. Use for reporting questions about deal volume.
    owner: carolyn
    version: 1
    definition_hash: "sha256:3f2c8a91d4e57b60c1a8f3927d5e6b40a29f18c7d3b5e0a4f6c9d2e8b7a10f53"
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/pipeline-summary
      auth: header
      secret_ref: env:N8N_WEBHOOK_TOKEN_REPORTS
    input_schema:
      type: object
      additionalProperties: false
      required: [start_date, end_date]
      properties:
        start_date: { type: string, format: date }
        end_date:   { type: string, format: date }
        stage:
          type: string
          enum: [prospecting, qualified, proposal, closed_won, closed_lost]
    tags: [reports, sales]
```

That entry is auto-approving, which is legal **only** because `side_effects: read_only`
(rule R5). Every other classification requires a human per invocation.

---

## 3. Choosing the classifications

These two fields carry the policy weight. Get them wrong and the rest of the system
enforces the wrong thing correctly.

### 3.1 `side_effects`

| Value | Means | Test |
|---|---|---|
| `read_only` | Changes nothing outside n8n. Running it twice is indistinguishable from running it once. | *If this ran a thousand times by accident, would anything be different?* No → `read_only`. |
| `external_write` | Creates or modifies state in a downstream system, but the change can be found and undone. | A CRM upsert, a spreadsheet row, a draft. |
| `irreversible` | Sends, pays, deletes, publishes, or otherwise cannot be recalled. | An email that leaves the building. A payment. A hard delete. |

Be honest about `read_only`. A workflow that "just reads" but writes a log row, bumps a
rate limit, or triggers a downstream webhook is `external_write`. When unsure, choose
the stronger class — the cost is one approval click; the cost of the reverse is real.

### 3.2 `risk`

Advisory metadata, shown to the model at discovery and to the human at approval. It does
not gate execution on its own, with one exception: `risk: high` forces
`approval: required` and `defaults` cannot weaken it (rule R10).

| Value | Use for |
|---|---|
| `low` | Small blast radius. Wrong output is an inconvenience. |
| `medium` | Real but recoverable consequences: wrong data in a system of record. |
| `high` | Money, external communication, deletion, or anything touching customers directly. |

### 3.3 The combination that matters

| `side_effects` | `approval: none` allowed? |
|---|---|
| `read_only` | Yes — the only case. |
| `external_write` | No (R5). |
| `irreversible` | No (R5). |

Unattended execution (T05) needs **both** `side_effects: read_only` and `approval: none`.
Neither alone is enough, and the default stays `approval: required`: you opt into
unattended running deliberately, per workflow.

When you do, preflight emits an `UNATTENDED_EXECUTION` warning on every check. It is not
a complaint about your entry — it states the actual trust relationship. Operator has no way
to read a node graph and confirm that a workflow only reads; it is trusting **your**
`side_effects` classification, and that classification is the only thing standing between an
agent and an unsupervised run. This is why section 3.1 asks you to choose the stronger class
when unsure.

---

## 4. Writing `input_schema`

JSON Schema draft 2020-12. Two hard requirements: it must be an object schema, and it
must set `additionalProperties: false` (rule R4). Beyond that, the schema is your
narrowest description of a valid call — and it is doing double duty, because the model
reads it to construct calls and the server uses it to reject them.

**Constrain aggressively.** Every constraint you add is a class of mistake the model
cannot make.

```yaml
input_schema:
  type: object
  additionalProperties: false
  required: [email, tier]
  properties:
    email:
      type: string
      format: email
      maxLength: 254
      description: Contact's primary email. Used as the upsert key.
    tier:
      type: string
      enum: [free, pro, enterprise]
      description: Subscription tier to set on the contact.
    notes:
      type: string
      maxLength: 2000
      description: Optional free-text note appended to the contact record.
    send_welcome:
      type: boolean
      default: false
      description: >
        When true, the workflow sends a welcome email. This is irreversible —
        prefer false unless the user explicitly asked to send one.
```

**Write descriptions for a model reader.** They are the only prose the model has when
choosing values. Say what a field means, what the units are, what the default behavior
is, and — as above — when *not* to set it. A good description prevents a bad call more
reliably than a validation error corrects one.

**Prefer enums to free strings**, `maxLength` on every string, `minimum`/`maximum` on
every number, and `format` where one applies. An unbounded string field is an invitation.

---

## 5. Computing `definition_hash`

> `registry hash` has two modes. Called with no arguments, it prints the registry
> **document's** own canonical content hash (BUILD_PLAN section 6.7) — the hash
> `registry validate` and `registry reload` use to detect whether the file changed, and
> the one shipped in phase 2. Called with `--n8n-workflow-id` (below), it computes one
> *workflow's* `definition_hash` by fetching its live definition from n8n — this mode
> requires n8n integration and is not yet implemented; it arrives in phase 4. Passing
> `--n8n-workflow-id` before then reports that plainly rather than silently doing nothing.

The hash pins the n8n workflow definition you reviewed. Operator canonicalizes the
definition and takes `sha256` over the canonical form.

```bash
n8n-operator registry hash --n8n-workflow-id 7Qx4kLmN2pRstUvW
# sha256:3f2c8a91d4e57b60c1a8f3927d5e6b40a29f18c7d3b5e0a4f6c9d2e8b7a10f53
```

Paste the output into `definition_hash`.

**Canonicalization is conservative** ([ADR-008](adr/ADR-008-conservative-definition-canonicalization.md),
rules CAN-01–CAN-07). Every field of the definition contributes to the hash unless it is on
an explicit exclusion allowlist, and a field joins that allowlist only after an empirical
harness proves that changing it cannot change what the workflow does. The two failure
directions are not equal: a hash that is too sensitive costs you a re-review, while a hash
that is too permissive lets a semantic change through unnoticed and executes a graph nobody
read.

**What that means for you in practice.** Early versions ship with a small exclusion set —
possibly empty — so edits you consider cosmetic *may* change the hash. Re-hashing is one
command. As the harness accumulates evidence, the noisy cases narrow. Expect this to
improve over time, and do not expect a documented list of "safe" edits until the evidence
exists to back one.

**When it changes.** Editing the workflow in n8n changes the hash. Every subsequent
preparation is `BLOCKED` with `DEFINITION_DRIFT`, and any already-approved operation is
refused at execute (AC-06, AC-13). This is working as intended: the approval you gave was
for the graph you read.

To adopt a change: review the new definition in n8n, re-run `registry hash`, update
`definition_hash`, bump `version`, and reload. In v2, `diff_workflow_definition` shows you a
structural diff so the review is a diff review rather than a re-read.

**Do not route around drift.** If re-hashing starts to feel like a formality you perform
without reading, that is the control degrading — say so, and let the harness fix the noise
at its source. The exclusion allowlist deliberately lives in code, under review, and is not
configurable per workflow: the person under drift-check pressure must not be able to
disable the drift check.

---

## 6. Secrets

Never put a secret in this file. `secret_ref` takes an **indirect reference**, and a
literal value is a load-time error (rule R6, [ADR-006](adr/ADR-006-server-owned-n8n-credentials.md)).

```yaml
trigger:
  auth: header
  secret_ref: env:N8N_WEBHOOK_TOKEN_CRM     # resolved from the environment
  # secret_ref: keyring:n8n-operator/crm    # or from the OS keyring
```

The registry is meant to live in version control. Everything in it — titles, schemas,
hashes, risk classes — should be reviewable in a pull request without leaking anything.

Likewise, `trigger.path` is a **path only**. The base URL comes from server
configuration, so the same registry works across environments and never records where
your n8n instance lives (rule R8).

---

## 7. Redaction

`output.redact` takes JSONPath expressions. Matched values are replaced with
`"[REDACTED]"` before the result leaves the process — before it is returned to the
model, and before it is written to the database.

```yaml
output:
  redact:
    - "$.contact.email"
    - "$.contact.phone"
    - "$..api_key"          # recursive: any api_key at any depth
    - "$.records[*].ssn"    # every element of an array
  max_bytes: 32768
  include_node_trace: false
```

Redact anything the model does not need in order to do its job. The model usually needs
to know *that* a contact was updated, not the contact's phone number. Note that
`describe_workflow` publishes only the *count* of redaction paths, never the paths
themselves.

`include_node_trace: true` lets `get_execution_log` return per-node data for this
workflow. It makes debugging much easier and widens what a result can carry — enable it
for workflows you are actively developing, and leave it off for anything touching
sensitive data.

---

## 7a. Execution correlation

A dispatch that times out leaves the operation `UNKNOWN`, and Operator will never guess
that it did not run ([ADR-009](adr/ADR-009-dispatch-correlation.md)). What decides whether
you can *reconcile* that afterwards is whether the workflow tells Operator which n8n
execution it was.

```yaml
trigger:
  type: webhook
  method: POST
  path: /webhook/crm-sync-contact
  auth: header
  secret_ref: env:N8N_WEBHOOK_TOKEN_CRM
  correlation: response_envelope     # default: none
```

To support it, shape the workflow's response node to return the Operator envelope:

```json
{ "n8n_operator": { "execution_id": "{{ $execution.id }}" },
  "data": { "…": "whatever your workflow already returns" } }
```

Operator unwraps `n8n_operator`, records the execution ID, and passes `data` through
redaction and shaping as the result. Your own consumers see `data` unchanged.

**Declaring `none` is fine.** The workflow stays fully executable and nothing is blocked.
What you give up is stated honestly rather than hidden: reconciliation after an `UNKNOWN`
is manual, and `get_execution_log` has less to show. Preflight reports this as a
non-blocking `warn` with code `NO_EXECUTION_CORRELATION`, so an approver sees the limitation
*before* deciding.

Add the envelope to anything where a duplicate or a lost execution would be expensive to
sort out — which is, in practice, exactly your `irreversible` workflows.

---

## 8. Limits

```yaml
limits:
  timeout_seconds: 30          # a slow workflow yields UNKNOWN, not a retry
  approval_ttl_seconds: 600    # how long a human has to decide
  execution_ttl_seconds: 120   # how long an approval stays executable
  max_concurrent: 1            # concurrent EXECUTING operations
  rate_limit_per_minute: 10
  max_argument_bytes: 8192     # optional; may only lower the server ceiling
```

Set `execution_ttl_seconds` short. It is the window between "a human approved this" and
"this ran" — the interval during which the world could change out from under the
approval. Minutes, not hours.

Set `timeout_seconds` slightly above the workflow's realistic worst case. A timeout
produces `UNKNOWN`, which requires a human to go check the downstream system, so an
over-tight timeout converts slow successes into manual work ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)).

`max_concurrent: 1` is the right default for anything that writes.

`max_argument_bytes` caps the canonical size of the arguments a caller may submit. The
server ceiling (`N8N_OPERATOR_MAX_ARGUMENT_BYTES`, 256 KiB by default) always applies; this
field may lower it for one workflow and may never raise it (rule R11,
[ADR-011](adr/ADR-011-argument-limits-and-idempotency.md)). Set it tight where you know the
shape of a valid call — a reporting workflow taking two dates has no business receiving a
megabyte. Oversized arguments are refused with `ARGUMENTS_TOO_LARGE` before anything is
persisted.

---

## 9. Operating the registry

### 9.1 Validate before you serve

```bash
n8n-operator registry validate --path ./workflows.yaml
```

Exits non-zero and names the offending entry and rule on any violation. Loading is
all-or-nothing: a bad registry never degrades into a partially-live allowlist, and the
server refuses to start (rule set R1–R10, AC-02). Run this in CI on the repository that
holds your registry.

### 9.2 Reloading

The registry is read at process start and on explicit reload. It is never re-read
mid-operation, so an operation always completes against the contract it was prepared
under (BUILD_PLAN section 6.7).

```bash
n8n-operator registry reload
```

Each successful load creates a snapshot with its own hash. Operations record their
snapshot, so an audit reader can reconstruct exactly which contract was in force.

### 9.3 Retiring a workflow

Set `enabled: false` rather than deleting the entry. The workflow disappears from
discovery and refuses preparation, while the entry remains for audit readers making
sense of historical operations.

### 9.4 Versioning

Bump `version` whenever the registered contract changes: the schema, the hash, the risk
class, the limits. It is the operator-facing changelog for an entry, and it appears in
`list_workflows` and `describe_workflow` so a model can notice it changed.

### 9.5 Multi-environment overlays

A workflow's contract — its `input_schema`, `side_effects`, `risk`, `title`,
`description`, `tags` — is the same everywhere it runs. What legitimately differs
between a `staging` n8n instance and a `production` one is *where a call actually
lands* and *how strict governance is about letting it land there*. An **overlay** is a
separate, per-environment file that adjusts exactly those things, on top of the one
base registry every environment shares (ADR-016).

**What an overlay may touch, and nothing else:**

- `n8n_workflow_id`, `definition_hash` — this environment's own instance may host the
  workflow under a different internal ID or a different (equally valid) build.
- `trigger.path`, `trigger.secret_ref` — a different webhook path or credential per
  instance.
- `approval_override: required` — require a human's approval here even when the base
  registry allows `approval: none`. There is no way to write the opposite: the schema
  simply has no `approval_override: none`.
- `limits_override` — tighten (never loosen) `execution_ttl_seconds`,
  `timeout_seconds`, `max_concurrent`, `rate_limit_per_minute`, `max_argument_bytes`
  (lower only), or `approval_ttl_seconds` (raise only — more deliberation time is the
  safer direction for that one field). A base value of `null` (no ceiling configured)
  can be set to any concrete value by an overlay; there is nothing to weaken.

Nothing else is possible: `input_schema`/`side_effects`/`risk`/`title`/`description`/
`tags` have no field on the overlay schema to even name, and setting an unknown key or
naming a `workflow_id` the base registry doesn't have is a load-time failure (rule
R13), the same all-or-nothing discipline `registry validate` already applies to the
base file. Attempting to weaken anything — raising a lower-only limit, lowering
`approval_ttl_seconds` — is rule R14 and also fails to load.

**Example.** `examples/environments/` ships three annotated files:

- `development.yaml` — an empty `overlays: []`. Development is the environment
  closest to the base registry's own policy, so most workflows need no override at
  all — that is the normal case, not a gap to fill in.
- `staging.yaml` — adds a `rate_limit_per_minute` the base registry never set, so a
  real, but lower-volume, load pattern can be exercised before production.
- `production.yaml` — requires approval for a workflow the base registry auto-approves
  (`reports.pipeline_summary`), and halves staging's own rate ceiling — a GTM engineer
  who validated a call in staging prepares the production equivalent under a real
  human approval gate and a tighter limit, deliberately harder to run by accident
  (ARCHITECTURE.md section 11.1).

**Validating and loading:**

```bash
n8n-operator environment validate-overlay <environment-id> --path ./staging.yaml
n8n-operator environment reload-overlay <environment-id> --path ./staging.yaml
```

`reload-overlay` replaces the full set of overlays that environment has: a workflow
overlaid before but no longer named in the file is no longer overridden, not silently
left with a stale prior override. Resolution of the merged (base + overlay) contract
happens live, at the moment each operation is prepared or executed — the same
snapshot-plus-live-check discipline the base registry's own drift detection already
uses — so a later overlay edit is picked up by the next call, never retroactively
rewriting what an already-prepared operation was actually governed by.

`n8n-operator environment registry-diff <environment-id> --path ./workflows.yaml`
shows, per workflow, exactly what this environment's resolved contract changes versus
the base — the fields an overlay may ever touch, and nothing else.

---

## 10. Authoring checklist

Before adding an entry:

- [ ] I have opened this workflow in n8n and read every node.
- [ ] I know what it touches downstream, and `side_effects` says so honestly.
- [ ] `risk` reflects the worst realistic outcome, not the typical one.
- [ ] `input_schema` sets `additionalProperties: false` and constrains every field.
- [ ] Every field description would make sense to someone who has never seen the workflow.
- [ ] `definition_hash` came from `registry hash`, just now.
- [ ] No literal secret appears anywhere in the entry.
- [ ] `output.redact` covers every sensitive path in a realistic result.
- [ ] `execution_ttl_seconds` is minutes, not hours.
- [ ] `max_argument_bytes` reflects the largest call I actually expect.
- [ ] If a duplicate or lost run would be expensive, the workflow returns the correlation
      envelope and the entry declares `correlation: response_envelope`.
- [ ] If it is not `read_only`, it requires approval — and I am willing to be paged for it.
- [ ] If it *is* `approval: none`, I accept that Operator is trusting my `read_only`
      classification with no human in the loop.
- [ ] `n8n-operator registry validate` passes.

---

## 11. Worked example: an irreversible workflow

```yaml
  - id: comms.send_customer_email
    n8n_workflow_id: "Kp9RtY2mNqXvB4Lc"
    title: Send an email to a customer
    description: >
      Sends a single email to one customer address through the transactional
      provider. The email leaves immediately and cannot be recalled. Use only when
      the user has explicitly asked to send a message and has seen the exact body.
    owner: carolyn
    version: 2
    definition_hash: "sha256:7b1e4d0a95c3f28e6d4a17b09c5e83f2a6d19b47c0e5a3f81d6c297b4e0a538f"
    risk: high
    side_effects: irreversible
    approval: required          # forced by R5 and R10 regardless
    trigger:
      type: webhook
      method: POST
      path: /webhook/send-customer-email
      auth: header
      secret_ref: env:N8N_WEBHOOK_TOKEN_COMMS
      correlation: response_envelope   # irreversible: reconciliation must be exact
    input_schema:
      type: object
      additionalProperties: false
      required: [to, subject, body]
      properties:
        to:
          type: string
          format: email
          maxLength: 254
          description: Single recipient address. Exactly one; no CC or BCC.
        subject:
          type: string
          maxLength: 200
          description: Subject line, as it will appear. No templating is applied.
        body:
          type: string
          maxLength: 10000
          description: >
            Plain-text body, sent verbatim. Show this to the user for confirmation
            before preparing the operation.
    output:
      redact: ["$.to", "$..message_id"]
      max_bytes: 4096
      include_node_trace: false
    limits:
      timeout_seconds: 20
      approval_ttl_seconds: 600
      execution_ttl_seconds: 120
      max_concurrent: 1
      rate_limit_per_minute: 5
      max_argument_bytes: 16384
    tags: [comms, customer-facing]
```

Every choice here follows from `irreversible`: approval is mandatory, the execution window
is two minutes, concurrency is one, the rate limit is low, arguments are capped near the
schema's own maximum, and the description tells the model to get explicit human confirmation
of the body *before* preparing — belt and braces alongside the approval surface, which will
show the human the exact body anyway.

`correlation: response_envelope` matters most here. If a dispatch times out on a workflow
that sends email, "did it send?" is the only question that matters, and an execution ID is
what lets anyone answer it.
