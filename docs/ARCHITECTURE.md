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
           2. idempotency lookup on (principal, idempotency_key)     ─ I8
              └ hit + same fingerprint  → return existing operation
              └ hit + other fingerprint → IDEMPOTENCY_KEY_CONFLICT
           3. T01 → PREPARING  (operation row + event + audit, one txn)
           4. registry.validation: args vs JSON Schema 2020-12
              └ fail → T02 INVALID (errors carry JSON-Pointer paths)
           5. n8n.preflight: reachable · active · definition hash · credentials
              └ fail → T03 BLOCKED (finding recorded)
           6. approval policy from the registry entry
              ├ required          → T04 PENDING_APPROVAL + approval token + URL
              └ none + read_only  → T05 APPROVED + execution deadline
       → shaped result: {operation_id, state, approval_url?, deadlines}
```

Steps 4 and 5 are the point of the product: nothing reaches n8n until the arguments
are provably schema-valid, and nothing is offered for approval until the target is
provably the workflow that was registered.

### 4.2 Approve (out-of-band, never MCP)

```
human → browser → 127.0.0.1 approval app
       GET  /approve/{token}   render: workflow, title, risk, side-effect class,
                                       full arguments, drift status, deadline
       POST /approve/{token}   T06 → APPROVED   (token verified, burned, TTL checked)
       POST /reject/{token}    T07 → REJECTED
```

The token is delivered in the `prepare` result as a URL, but possessing the URL is not
authority — a human must act on the page. No MCP tool can reach these routes
(boundary B4). The page shows the arguments verbatim, so a human sees exactly what a
manipulated model asked for.

### 4.3 Execute

```
client → mcp/tools.execute_operation(operation_id, handle)
       → core.service.execute_operation
           1. load operation; state must be APPROVED         else APPROVAL_REQUIRED
           2. verify handle binding: principal + workflow + argument fingerprint  ─ I5
           3. now <= execution_deadline                       else OPERATION_EXPIRED
           4. re-check definition hash against live n8n       else DEFINITION_DRIFT  ─ B8
           5. burn handle: UPDATE ... WHERE handle_burned_at IS NULL
              └ affected rows = 0 → HANDLE_ALREADY_USED       ─ I4
           6. T10 → EXECUTING (committed before dispatch)
           7. dispatch to n8n with the registry timeout, no retry  ─ ADR-005
           8. outcome →  success       T13 SUCCEEDED
                         error         T14 FAILED
                         indeterminate T15 UNKNOWN
           9. persist redacted, size-capped result
       → shaped result
```

Step 4 re-checks drift because approval and execution are separated in time. Approving
workflow *X* does not authorize running whatever now sits at *X*'s ID.

Step 6 commits `EXECUTING` **before** the network call. If the process dies mid-flight,
recovery finds an operation stuck in `EXECUTING` and resolves it to `UNKNOWN` — it never
finds an approved operation of undetermined disposition.

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
| Approval token | Server | yes, as a URL | Single-use, TTL-bounded, stored only as a hash. |
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
- SQLite runs in WAL mode with a busy timeout; concurrency correctness never relies on
  SQLite's single-writer behavior, because Postgres will not provide it.

### 6.2 Audit chain

`audit/writer.py` is the only module that inserts into `audit_log`. Each entry hashes
the canonical serialization of its own fields together with the previous entry's hash.
`audit/chain.py` verifies a range and reports the first break by sequence number.
Tamper-evidence, not tamper-proofing — see BUILD_PLAN section 9.4.

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
| `N8N_OPERATOR_LOG_LEVEL` | `INFO` | Structured JSON logs with secret scrubbing. |

---

## 8. Processes

v1 runs as one to three processes over one SQLite database:

| Process | Command | Lifetime |
|---|---|---|
| MCP stdio server | `n8n-operator serve stdio` | Spawned per client session by the MCP host. |
| MCP HTTP server | `n8n-operator serve http` | Long-running, optional. |
| Approval app | `n8n-operator serve approval` | Long-running whenever approvals are in use. |

Expiry (T08, T11) is handled by a sweeper that runs inside the approval app and,
defensively, as a lazy check on every operation read — so an expired operation reads as
`EXPIRED` even when no sweeper has run. Deployment topology is intentionally boring in
v1: no queue, no worker pool, no scheduler.

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

---

## 11. What this architecture deliberately does not do

- **No plugin system.** Extensibility would mean loading operator-supplied code into
  the trusted zone. The registry is the extension mechanism.
- **No caching of n8n state.** Preflight and the execute-time drift check must reflect
  reality, not a cache.
- **No background execution queue.** Asynchronous dispatch would separate the human
  approval from the run in ways v1 cannot audit cleanly.
- **No generic passthrough.** There will never be an `n8n_request` tool, in any version.
