# n8n Operator — Architecture

> Companion to [BUILD_PLAN.md](BUILD_PLAN.md), which is normative for the state
> machine (section 5), registry schema (section 6), tool inventory (section 7),
> storage model (section 8), and security boundaries (section 9). This document
> explains *how the code is arranged* to satisfy those definitions and does not
> restate them.

---

## 1. Architectural stance

Three commitments shape every structural decision:

1. **The domain is not the protocol.** MCP is one way to reach the core. The CLI is
   another, the approval app a third, and a future HTTP API a fourth. Governance logic
   lives in `core/` and knows nothing about any of them ([ADR-001](adr/ADR-001-portable-mcp-core.md)).
2. **Authority is a data structure, not a code path.** What may run is a registry
   snapshot row. Whether *this* run may proceed is an operation row plus a handle.
   Neither is a conditional buried in a request handler ([ADR-002](adr/ADR-002-default-deny-registry.md), [ADR-003](adr/ADR-003-operation-handles.md)).
3. **Every decision leaves a record before it takes effect.** State transition,
   event row, and audit row commit in one transaction. If the audit write fails, the
   transition did not happen.
4. **Correctness does not depend on a process being up.** Expiry is applied lazily inside
   the transaction that reads or acts on an operation, so no deployment topology can make
   an expired approval executable ([ADR-010](adr/ADR-010-approval-delivery-and-expiry.md)).
   Sweepers and maintenance commands improve audit-timeline fidelity, never safety.

---

## 2. Component map

```
                          Zone A — untrusted
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │Claude Desktop│   │   ChatGPT    │   │Codex / other │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │ stdio            │ Streamable HTTP  │
          └──────────────────┴──────────────────┘
                             │
════════════════════════════ │ ═══════════════ trust boundary ══════════════
                             ▼          Zone B — n8n Operator (trusted)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Adapters (thin — translate, never decide)                       │
   │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐         │
   │  │ mcp/          │  │ cli/          │  │ approval/     │         │
   │  │ server.py     │  │ main.py       │  │ app.py        │         │
   │  │ tools.py      │  │ commands/     │  │ routes.py     │         │
   │  │ resources.py  │  │  (Typer)      │  │  (FastAPI,    │         │
   │  │ transports.py │  │               │  │   loopback)   │         │
   │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘         │
   │          └──────────────────┼──────────────────┘                 │
   │                             ▼                                    │
   │  ┌────────────────────────────────────────────────────────────┐  │
   │  │ core/service.py — use cases, the portable core             │  │
   │  │   prepare · approve · execute · inspect · cancel           │  │
   │  └───┬──────────┬──────────┬──────────┬──────────┬────────────┘  │
   │      ▼          ▼          ▼          ▼          ▼               │
   │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐          │
   │  │ state_ │ │handles │ │idempo- │ │redac-  │ │ models │  core/   │
   │  │machine │ │        │ │ tency  │ │ tion   │ │        │          │
   │  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘          │
   │      │          │          │          │          │               │
   │      ▼          ▼          ▼          ▼          ▼               │
   │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐             │
   │  │ registry/│ │ storage/ │ │  audit/  │ │   n8n/   │             │
   │  │ loader   │ │ models   │ │  chain   │ │  client  │             │
   │  │ schema   │ │ repo     │ │  writer  │ │ preflight│             │
   │  │validation│ │ session  │ │          │ │  types   │             │
   │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘             │
   └───────┼────────────┼────────────┼────────────┼───────────────────┘
           ▼            ▼            ▼            │
     workflows.yaml   SQLite    audit chain       │
                    (Postgres in v2)              │
════════════════════════════════════════════════ │ ══════════════════════
                                                  ▼   Zone C — privileged
                                           ┌──────────────┐
                                           │ n8n instance │
                                           └──────┬───────┘
                                                  ▼   Zone D
                                        downstream SaaS / systems
```

### 2.1 Layer responsibilities

| Layer | Modules | May do | Must not do |
|---|---|---|---|
| **Adapter** | `mcp/`, `cli/`, `approval/` | Parse and validate transport-level input, call one `core.service` use case, shape the result for the transport. | Decide policy, touch the database, call n8n, or write audit records. |
| **Core** | `core/` | Orchestrate use cases, apply the state machine, mint and burn handles, compute fingerprints, redact output, and commit transitions with their audit records. | Import any adapter, `fastapi`, `typer`, or the MCP SDK. |
| **Capability** | `registry/`, `storage/`, `audit/`, `n8n/` | Own one external concern each: the allowlist, persistence, the audit chain, the n8n API. | Depend on each other or on `core/`. |

The dependency graph is a DAG pointing inward. A contract test walks the import graph
and fails the build on a violation (BUILD_PLAN section 10.3).

### 2.2 Why the adapters are thin

The MCP adapter is the attack surface. Keeping it translation-only means the security
argument does not have to reason about protocol details: whatever a client sends, it
reaches the core as a validated argument object, and the core's behavior is identical
whether the caller was Claude over stdio, a remote client over Streamable HTTP, or the
operator's own CLI. It also makes the whole governance layer unit-testable without a
protocol in the loop.

---

## 3. The MCP adapter

Built on the official MCP Python SDK v2 (`mcp >= 2.1, < 3`).

```
mcp/
├── server.py       # constructs MCPServer, registers tools and resources
├── tools.py        # 12 v1 tools; Pydantic v2 argument models; -> core.service
├── resources.py    # registry://workflows, audit://operations/{id}
└── transports.py   # stdio and Streamable HTTP entry points + bind guard
```

`server.py` builds a single `mcp.server.MCPServer` and registers the same tool set
regardless of transport, so the surface is provably identical across hosts (AC-23).

### 3.1 Transports

| Transport | Default | Use | Guard |
|---|---|---|---|
| **stdio** | yes | Claude Desktop and any host that launches a subprocess. | The parent process is the security boundary; no network listener exists. |
| **Streamable HTTP** | opt-in | Remote MCP clients. | Binds `127.0.0.1` by default. A non-loopback bind requires a bearer token **and** an `Origin` allowlist, or startup fails (boundary B9, AC-20). |

The `Origin` allowlist is DNS-rebinding defense: without it, a page in the operator's
browser could reach a loopback-bound MCP server and drive it.

### 3.2 Response shaping

Every tool result passes through a single shaping function before serialization. It
projects onto an explicit allowlist of fields rather than filtering a denylist, so a
new internal field is invisible by default rather than leaked by default. This is what
makes boundary B5 testable as a property rather than a review checklist.

---

## 4. Request flows

### 4.1 Prepare

```
client → mcp/tools.prepare_operation
       → core.service.prepare_operation
           1. resolve workflow_id in the active registry snapshot   ─ ADR-002
              └ miss → WORKFLOW_NOT_FOUND (no operation created)
           2. canonicalize arguments; size check vs effective limit  ─ ADR-011 · I10 · B12
              └ over  → ARGUMENTS_TOO_LARGE (nothing persisted)
           3. idempotency lookup on the namespace                    ─ I8
              (principal, environment, workflow_id, idempotency_key)
              └ hit + same fingerprint  → return existing operation
              └ hit + other fingerprint → IDEMPOTENCY_CONFLICT
           4. T01 → PREPARING  (operation row + event + audit, one txn)
           5. registry.validation: args vs JSON Schema 2020-12
              └ fail → T02 INVALID (errors carry JSON-Pointer paths)
           6. n8n.preflight: reachable · active · definition hash ·
              credential bindings · correlation                      ─ ADR-009
              └ fail  → T03 BLOCKED (finding recorded)
              └ warn / unverifiable → recorded, does not block
           7. approval policy from the registry entry
              ├ required          → T04 PENDING_APPROVAL + approval token
              └ none + read_only  → T05 APPROVED + execution deadline
           8. caller locality decides approval delivery              ─ ADR-010 · I12 · B13
              ├ local  → approval_required + instructions + approval_url
              └ remote → approval_required + instructions (no URL)
       → shaped result: {operation_id, state, approval_required,
                         approval_instructions, approval_url?, deadlines}
```

Steps 5 and 6 are the point of the product: nothing reaches n8n until the arguments are
provably schema-valid, and nothing is offered for approval until the target is provably the
workflow that was registered. Step 2 precedes the write deliberately — an oversized payload
is refused before it can be persisted, which is what makes threat T-12 a real mitigation
rather than a transport-dependent one.

### 4.2 Approve (out-of-band, never MCP)

Two channels, one core use case ([ADR-010](adr/ADR-010-approval-delivery-and-expiry.md)).

**Canonical — the CLI, on the Operator machine:**

```
human → n8n-operator operations show op_01JQ…      render: workflow, title, risk,
                                                   side-effect class, full arguments,
                                                   drift status, deadline
      → n8n-operator operations approve op_01JQ…   T06 → APPROVED
      → n8n-operator operations reject  op_01JQ…   T07 → REJECTED
```

**Convenience — the loopback approval page, when one is running:**

```
human → browser → 127.0.0.1 approval app
       GET  /approve/{token}   render: the same decision surface
       POST /approve/{token}   T06 → APPROVED   (token verified, burned, TTL checked)
       POST /reject/{token}    T07 → REJECTED
```

Both call `core.service`, so neither is a second implementation of policy, and both are
outside the MCP channel — there is still no tool that approves (boundary B4). The CLI is
canonical because it works in every v1 topology, including headless and remote-HTTP
deployments where no browser can reach loopback.

Where a URL is issued, possessing it is not authority: a human must act. `GET` renders and
grants nothing; approval is a `POST` with a CSRF token. Both surfaces show the arguments
verbatim, so a human sees exactly what a manipulated model asked for.

### 4.3 Execute

```
client → mcp/tools.execute_operation(operation_id, handle)
       → core.service.execute_operation
           0. apply any overdue T08/T11 in this transaction   ─ ADR-010 · I9
           1. load operation; state must be APPROVED          else APPROVAL_REQUIRED
                                                              (EXPIRED after step 0 →
                                                               OPERATION_EXPIRED)
           2. verify handle binding: principal + workflow + argument fingerprint  ─ I5
           3. now <= execution_deadline                       else OPERATION_EXPIRED
           4. re-check definition hash against live n8n       else DEFINITION_DRIFT  ─ B8
           5. burn handle: UPDATE ... WHERE handle_burned_at IS NULL
              └ affected rows = 0 → HANDLE_ALREADY_USED       ─ I4
           6. T10 → EXECUTING (committed before dispatch)
           7. dispatch to n8n with the registry timeout, no retry  ─ ADR-005
           8. outcome →  success       T13 SUCCEEDED
                         error         T14 FAILED
                         indeterminate T15 UNKNOWN            ─ ADR-009
                         (never inferred to be a non-event)
           9. unwrap the response envelope, if declared;
              record n8n execution ID where present           ─ ADR-009
          10. persist redacted, size-capped result
       → shaped result
```

Step 4 re-checks drift because approval and execution are separated in time. Approving
workflow *X* does not authorize running whatever now sits at *X*'s ID.

Step 6 commits `EXECUTING` **before** the network call. If the process dies mid-flight,
recovery finds an operation stuck in `EXECUTING` and resolves it to `UNKNOWN` — it never
finds an approved operation of undetermined disposition.

Step 8 is where the temptation lives. A timeout means no response arrived inside the
window; it does not mean nothing happened. There is no code path that reads an exception
class or an elapsed time and concludes the workflow did not run
([ADR-009](adr/ADR-009-dispatch-correlation.md)). Step 9 is what makes an `UNKNOWN`
reconcilable *when the workflow was authored to support it* — and its absence costs
reconciliation, never safety.

---

## 5. Data flow and trust

| Datum | Origin | Crosses to client? | Treatment |
|---|---|---|---|
| Registry ID | Operator | yes | The only workflow identifier a client ever sees. |
| `n8n_workflow_id` | Operator | **never** | Server-side only; excluded by the response allowlist. |
| Instance URL | Config | **never** | Server-side only. |
| API key / webhook secret | Env or keyring | **never** | Resolved at startup, held in memory, scrubbed from logs (ADR-006). |
| Tool arguments | Client (untrusted) | echoed back | Schema-validated, fingerprinted, redacted before persistence. |
| Operation handle | Server | yes | Opaque `op_<ULID>`; carries no authority by itself until bound and approved (ADR-003). |
| Approval token | Server | as a URL, **local callers only** | Single-use, TTL-bounded, stored only as a hash. Withheld from remote callers (I12, B13). |
| n8n execution ID | n8n, via the response envelope | yes, in `correlation` | An *execution* identifier, not a workflow identifier; used for reconciliation and debugging (ADR-009). |
| Execution result | n8n (untrusted) | yes | Redacted, size-capped, structurally shaped. |
| Audit record | Server | only via `audit://` and CLI export | Append-only, hash-chained. |

**n8n output is untrusted input.** A workflow can return whatever a downstream system
gave it, including text engineered to steer the model reading the result. Operator
does not sanitize semantics — it cannot — but results are structurally shaped and
delivered as data, never as instructions, and no tool interprets a result as a request
to do anything further. Every subsequent side effect still requires its own prepare,
its own human approval, and its own handle.

---

## 6. Persistence

SQLAlchemy 2.0 ORM (typed `Mapped[...]` declarative style), Alembic for every schema
change, SQLite in v1 and PostgreSQL in v2 ([ADR-004](adr/ADR-004-sqlite-to-postgres.md)).
Table definitions are in BUILD_PLAN section 8.1.

### 6.1 Transaction rules

- A state transition, its `operation_events` row, and its `audit_log` row commit in a
  single transaction (I6). No partial governance record can exist.
- The handle burn is a conditional `UPDATE` with a checked affected-row count — a
  compare-and-set, not a read-then-write (I4).
- Every mutation of `operations` carries `state_version` as an optimistic-concurrency
  guard; a stale version aborts the transaction rather than overwriting.
- Every read of, and action on, an operation applies any overdue T08 or T11 first, in the
  same transaction, and writes its event and audit rows like any other transition — lazy
  expiry is a real transition, not a display convention (I9). It is idempotent and
  race-safe under the `state_version` guard.
- The canonical argument size is checked before the `operations` row is written, so an
  oversized payload never reaches the database (I10).
- SQLite runs in WAL mode with a busy timeout; concurrency correctness never relies on
  SQLite's single-writer behavior, because Postgres will not provide it.

### 6.2 Audit chain

`audit/writer.py` is the only module that inserts into `audit_log`. Each entry hashes
the canonical serialization of its own fields together with the previous entry's hash.
`audit/chain.py` verifies a range and reports the first break by sequence number.
Tamper-evidence, not tamper-proofing — see BUILD_PLAN section 9.4.

### 6.3 PostgreSQL (v2, stage 01)

`storage/session.py`'s `create_engine_for_url` builds a dialect-appropriate engine: the
SQLite pragmas above on SQLite, a bounded `QueuePool` with `pool_pre_ping` and
`pool_recycle` plus a per-connection `statement_timeout` and an explicit `SET TIME ZONE
'UTC'` on PostgreSQL — all connection/engine-setup concerns, none of it schema
(the same "configured at connection setup only" discipline the SQLite pragmas already
follow, ADR-004 rule D9, extended to the second dialect). Every value is a `Settings`
field (`database_pool_size`, `database_statement_timeout_seconds`, etc.) — see
[POSTGRES_OPERATIONS.md](POSTGRES_OPERATIONS.md) for tuning guidance and connection
budgeting across concurrent processes.

`storage/health.py`'s `check_database_health` opens one connection, times a trivial
query, and reports reachability/latency/pool occupancy — never `database_url` itself
(that's `config.redact_database_url`'s job, used everywhere the URL is displayed:
`db status`, `migrate-to-postgres`'s own output). `db status` calls both.

`storage/session.py`'s `run_in_session_with_retry` retries a DB-only transaction on a
transient PostgreSQL deadlock or serialization failure (or SQLite lock contention) with
a fresh session, bounded attempts, and no retry of anything that is not itself a
transient error (a constraint violation propagates on the first attempt). It is not
wired into `core/service.py`'s existing `prepare_operation`/`execute_operation` paths —
a deliberate scope boundary for this stage, not an oversight — but is used by the
SQLite-to-PostgreSQL migration tool's row-copy loop, where retrying a failed chunk is
unambiguously safe (no external side effect has occurred; see ADR-005's no-automatic-
retry discipline, which this primitive is careful never to cross into the n8n-dispatch
boundary).

`core/postgres_migration.py` orchestrates the one-time data copy: `storage/
postgres_migration.py` (a `storage`-capability module, so it may not import `audit/` or
`core/` — ARCHITECTURE.md section 2.1) copies rows and reports counts; `core/
postgres_migration.py` composes that with an independent re-verification of the
destination's audit hash chain (`core/service.py`'s existing `verify_audit_chain`), the
same layering split every other cross-capability use case in this codebase follows. See
[POSTGRES_OPERATIONS.md](POSTGRES_OPERATIONS.md) for the operator-facing walkthrough.

---

## 7. Configuration

Pydantic v2 `BaseSettings`, `N8N_OPERATOR_` prefix, validated at process start. A
malformed or incomplete configuration is a startup failure, never a runtime surprise.

| Setting | Default | Notes |
|---|---|---|
| `N8N_OPERATOR_N8N_BASE_URL` | — | Required. Never returned by any tool. |
| `N8N_OPERATOR_N8N_API_KEY` | — | Required. Env or keyring reference. |
| `N8N_OPERATOR_REGISTRY_PATH` | `./workflows.yaml` | Load fails closed. |
| `N8N_OPERATOR_DATABASE_URL` | `sqlite+pysqlite:///./n8n-operator.db` | Postgres URL in v2. |
| `N8N_OPERATOR_APPROVAL_BIND` | `127.0.0.1:8765` | Non-loopback rejected in v1. |
| `N8N_OPERATOR_HTTP_BIND` | `127.0.0.1:8000` | Non-loopback requires token + Origin allowlist. |
| `N8N_OPERATOR_HTTP_BEARER_TOKEN` | unset | Required for non-loopback HTTP. |
| `N8N_OPERATOR_HTTP_ALLOWED_ORIGINS` | unset | Required for non-loopback HTTP. |
| `N8N_OPERATOR_REQUEST_TIMEOUT_SECONDS` | `60` | Ceiling; per-workflow `limits` may lower it. |
| `N8N_OPERATOR_MAX_ARGUMENT_BYTES` | `262144` | Server ceiling on canonical argument size. `limits.max_argument_bytes` may lower it per workflow, never raise it (rule R11, ADR-011). |
| `N8N_OPERATOR_APPROVAL_URL_EXPOSURE` | `auto` | `auto` includes an approval URL for local callers only; `never` suppresses it everywhere. It can never force exposure to a remote caller (I12). |
| `N8N_OPERATOR_LOG_LEVEL` | `INFO` | Structured JSON logs with secret scrubbing. |

---

## 8. Processes

v1 runs as one to three processes over one SQLite database:

| Process | Command | Lifetime |
|---|---|---|
| MCP stdio server | `n8n-operator serve stdio` | Spawned per client session by the MCP host. |
| MCP HTTP server | `n8n-operator serve http` | Long-running, optional. |
| Approval app | `n8n-operator serve approval` | Long-running, **optional** — the CLI is the canonical approval channel (ADR-010). |

### 8.1 Expiry

**Lazy transactional expiry is authoritative.** Every read of, and action on, an operation
applies any overdue T08 or T11 before evaluating state, in the same transaction (invariant
I9). No expired operation can be executed in any topology, because the act of executing it
expires it first.

Two optional mechanisms improve the *timing* of the audit record, never the safety:

| Mechanism | Role |
|---|---|
| Sweeper inside the approval app | Best-effort. Writes `EXPIRED` near the wall-clock deadline instead of at next touch. Nothing depends on it. |
| `n8n-operator operations expire` | Explicit maintenance, for cron or a systemd timer in deployments that run no approval app. |

**The stdio-only consequence.** With no approval app and no scheduled maintenance command,
an operation that expires is still *treated* as expired the instant anything touches it,
but its `EXPIRED` audit event carries the timestamp of that touch rather than of the
deadline — and an operation nobody touches again may never receive one. This is a fidelity
limitation of the audit timeline, not of safety, and it is recorded in BUILD_PLAN section
9.5 so it is never discovered as a surprise gap in an audit export.

Deployment topology is otherwise intentionally boring in v1: no queue, no worker pool, no
scheduler.

---

## 9. Error model

One error taxonomy, defined normatively in [MCP_TOOLS.md](MCP_TOOLS.md) section 4 and
implemented once in `errors.py`. Every error carries a stable machine-readable `code`,
a human-readable `message`, and optional structured `details`. Adapters map the same
core exceptions to their own conventions — MCP tool errors, CLI exit codes, HTTP
status — without inventing new codes.

Errors are written to be *model-actionable*: they say what was wrong, where, and what
would make the call succeed. `INVALID_ARGUMENTS` returns JSON-Pointer paths so a model
can repair its own call without guessing.

---

## 10. Extension points

Designed in now, unimplemented until their version:

| Point | Enables | Version |
|---|---|---|
| `Principal` on every use case | OAuth/OIDC identity and RBAC without threading a new parameter | v2 |
| `environment` on registry entries and operations | Multiple n8n instances | v2 |
| `parent_operation_id` on `operations` | Governed retries that link, never mutate | v2 |
| Approval as a policy object, not a boolean | N-of-M quorum | v2 |
| Registry snapshots as content-addressed documents | Compiled workflow sources | v3 |
| `definition_hash` canonicalization | Structural diffs and the evaluation lab | v2 / v3 |
| `AuditAnchor` interface (`publish` / `verify`, content-free anchors) | External audit anchoring: signed local file and authenticated HTTPS webhook in v2; KMS, transparency log, WORM in v3 ([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md)) | v2 / v3 |
| `trigger.correlation` on registry entries | Exact-ID reconciliation of `UNKNOWN` operations, as audit annotations only ([ADR-009](adr/ADR-009-dispatch-correlation.md)) | v2 |
| Canonicalization exclusion allowlist (versioned, evidence-backed) | Narrowing false drift as the compatibility harness proves fields cosmetic ([ADR-008](adr/ADR-008-conservative-definition-canonicalization.md)) | v1 phase 4 onward |

---

## 11. v2 user journeys

Three journeys the v2 outcome (BUILD_PLAN section 2.2) exists to serve, each walked
end to end against the contracts in [MCP_TOOLS.md](MCP_TOOLS.md) section 5 and the
ADRs cited inline. Each is also a row group in
[V2_TRACEABILITY.md](V2_TRACEABILITY.md).

### 11.1 A startup GTM engineer operating staging and production

A five-person startup has one n8n instance's workflows split across a `staging` and a
`production` environment in one organization. The GTM engineer holds `operator` scoped
to `crm.*` in both environments and `approver` scoped to `mkt.*` in `staging` only.

1. `whoami` — one organization, two environments, the two scoped roles above
   ([ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md),
   [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)).
2. `list_environments` — `staging` and `production`, neither archived.
3. `prepare_operation` for `crm.sync_contact` with no `environment` argument —
   `ENVIRONMENT_REQUIRED`: two environments exist, and production is never implicit
   ([ADR-016](adr/ADR-016-environment-registry-overlays.md) section 3). The engineer
   names `staging`; the workflow's `staging` overlay applies, its `approval: none`
   (unchanged from base) executes without waiting.
4. The same call against `production` — the base registry entry, no overlay for this
   workflow in `production`, requires human approval; `request_approval` routes to the
   org's `approver`s for `crm.*` in `production` — a role the GTM engineer does not
   hold there, so someone else on the team decides.
5. A `mkt.campaign_sync` operation prepared in `production` by anyone: the engineer,
   asked to approve it, is not in the operation's `approval_policy_snapshot` — their
   `approver` role is scoped to `staging` only — and `request_approval` correctly never
   notifies them for this one.

What this exercises: implicit-environment refusal (AC-37), workflow×environment
role-scope intersection (AC-39), and overlay-scoped approval policy differing by
environment for the identical workflow ID.

### 11.2 A RevOps team requiring two-person approval for a bulk CRM update

A RevOps team of four runs `crm.bulk_update_stage` — `side_effects: external_write`,
`risk: high` — against `production`, with an org policy requiring 2 of the team's 3
`approver`-scoped principals to sign off before any bulk update executes.

1. `prepare_operation` reaches `PENDING_APPROVAL`; the requester is one of the four
   and holds `approver` themselves, but the write-time
   `approval_policy_snapshot` structurally excludes them from their own request's
   eligible-approver list — no self-approval, by construction, not by a check that
   could be skipped ([ADR-017](adr/ADR-017-team-approval-quorum-semantics.md)
   section 1).
2. `request_approval` notifies the remaining three eligible approvers over the
   `NotificationSink` webhook — event type, operation ID, and a fetch reference only,
   never the bulk update's actual argument list
   ([ADR-018](adr/ADR-018-notification-and-alert-hook-delivery.md) section 4).
3. Two approvers decide `approve` via the CLI (the actual decision, out-of-band, never
   an MCP tool call — boundary B4 unchanged from v1); `get_approval_status` shows
   `quorum_count: 2`, two decisions in, `ready: true`. The operation moves `APPROVED`
   (T06).
4. The third approver, unaware quorum was already reached, later tries to decide
   anyway — irrelevant to quorum, but if they attempt a *second* decision on an
   operation they'd already decided earlier in a different scenario, that call returns
   `APPROVAL_ALREADY_DECIDED`, changing nothing (ADR-017 section 3).
5. Mid-approval, an admin removes one of the three approvers from the org for an
   unrelated reason. The snapshot is unaffected — quorum was already satisfied by the
   other two — and even if it had not been, the removed approver's un-cast slot would
   simply become unfillable, never re-expanding to admit a replacement
   (invariant I13, ADR-017 section 1).
6. `execute_operation` dispatches exactly once, identically to v1's single-approver
   path — quorum changes who must agree, never what happens after agreement.

What this exercises: self-dealing exclusion, snapshot immutability under mid-flight
membership churn, duplicate-decision rejection, and content-free notification delivery
(AC-40, AC-41, AC-49).

### 11.3 Marketing operations investigating campaign-sync drift or a failed enrichment run

A marketing-ops analyst holds `viewer` scoped to `mkt.*` across both environments — no
`operator` or `approver` — and is asked why last night's `mkt.enrich_leads` run failed
and whether `mkt.campaign_sync`'s live n8n definition still matches what is registered.

1. `list_audit_events` filtered to `mkt.enrich_leads`, cursor-paginated — every event
   the analyst is authorized to see for workflows in their scope, and nothing for any
   workflow outside it: an unauthorized workflow's events are absent from the result
   entirely, not present-and-redacted (ADR-012 section 3,
   [ADR-015](adr/ADR-015-rbac-authorization-evaluation.md)). The failed operation's ID
   surfaces here.
2. `get_execution_log` on that operation ID — the failing node and its error message,
   unchanged from v1 (AC-15) — enough to tell the analyst *what* broke without needing
   `operator` access to have caused it.
3. `get_metrics` for `mkt.*` over the `24h` window — success/failure counts and
   latency percentiles, pre-filtered to the analyst's authorized workflow set before
   any aggregation runs, so a struggling *other* team's workflow never appears in a
   total the analyst sees ([ADR-019](adr/ADR-019-metrics-cardinality-and-privacy.md)
   section 1). `mkt.enrich_leads`'s p95 shows `null` with
   `"reason": "insufficient_sample"` — it runs rarely enough that ten executions
   haven't accumulated in the window (ADR-019 section 4); the analyst reads the raw
   failure event from step 1–2 instead, which is exactly the tool for that.
4. `diff_workflow_definition` on `mkt.campaign_sync` against `production` — a
   structural diff against the registered `definition_hash`
   ([ADR-008](adr/ADR-008-conservative-definition-canonicalization.md)), confirming
   whether the sync workflow itself has drifted or whether last night's failure was
   transient. `viewer` is sufficient for this call — no write capability is needed to
   ask "did this change."

What this exercises: `viewer`-role read scope across the full v2 monitoring surface
without any `operator`/`approver` grant, metrics privacy filtering ahead of
aggregation, and the percentile sample-size floor surfacing an honest "not enough
data" rather than a misleading number (AC-42, AC-43, AC-44).

## 12. What this architecture deliberately does not do

- **No plugin system.** Extensibility would mean loading operator-supplied code into
  the trusted zone. The registry is the extension mechanism.
- **No caching of n8n state.** Preflight and the execute-time drift check must reflect
  reality, not a cache.
- **No background execution queue.** Asynchronous dispatch would separate the human
  approval from the run in ways v1 cannot audit cleanly.
- **No generic passthrough.** There will never be an `n8n_request` tool, in any version.
