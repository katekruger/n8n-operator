# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.0-rc2] - 2026-08-27

### Fixed

- Corrected the CI-only strict-mypy failure found after the rc1 tag.
- Aligned package, runtime, changelog, and repository metadata on rc2.
- Replaced the unpublished PyPI quickstart with a source-install path that works while
  the release candidate remains private.

### Changed

- Release status now says "release candidate" until repeatable live-n8n and real remote
  connector verification are complete.
- Added automated packaging, clean-install, coverage, security, and compatibility gates
  for public-release readiness.

## [1.0.0-rc1] - 2026-08-27

### Added — phase 9: v1 release preparation

Full release-readiness pass: every acceptance criterion AC-01–AC-25 mapped to its
verifying test, the full quality gate re-run clean (ruff, format, mypy strict, the
complete pytest suite, coverage gates, the import-graph layering contract, a manual
secret scan, a package build, a fresh-database migration, and end-to-end MCP smoke
tests over both stdio and Streamable HTTP against the built wheel in an isolated
environment), and `docs/THREAT_MODEL.md` reviewed against shipped code rather than
design intent.

- **Found and fixed a real pre-release bug**: nothing in the shipped product ever
  created the v1 default (`"local"`) principal row — every test passed only because
  test fixtures seeded it directly, bypassing the CLI path a real user takes. A
  genuinely fresh `db init` → `registry reload` → `prepare_operation` failed a
  `principals` foreign key on first use. `n8n-operator db init`/`db migrate` now seed
  it idempotently; regression tests added
  (`tests/integration/test_cli_db.py::test_a_genuinely_fresh_cli_only_install_can_prepare_an_operation`
  and neighbors).
- **Corrected AC-02's own wording** to match shipped behavior: `serve stdio`/
  `serve http` do not read the registry file or refuse to start on an invalid one —
  they serve from whatever snapshot `registry reload` already validated into the
  database (which is the actual default-deny enforcement point), and return
  `REGISTRY_UNAVAILABLE` per call, not at startup, when no snapshot exists.
- **Corrected three stale `THREAT_MODEL.md` entries** against actual implementation:
  T-35 (audit tampering detection) upgraded `partial` → `mitigated` now that
  `audit verify`/`audit export` are real, shipped, tested commands; T-36 (data at
  rest) corrected — operation arguments are stored **raw**, not redacted, since phase
  7 (dispatch and fingerprint re-verification need the real values); T-37
  (crash-stranded `EXECUTING`) downgraded `mitigated` → `partial` — v1 has no
  automatic or CLI-driven recovery for this case, only detection that it's stuck. New
  residual risk RR-10 and out-of-scope item 10 record the T-37 gap explicitly.
- `docs/V1_LIMITATIONS.md` (new): a plain-language index of every `partial`/`accepted`
  threat-model item and version-boundary limitation, with the practical consequence
  spelled out — not just the severity table.
- `docs/RECONCILING_UNKNOWN.md` (new): the step-by-step manual procedure for resolving
  an `UNKNOWN` operation (with and without an execution ID) and, separately, for the
  rarer crash-stranded-`EXECUTING` case — including the exact emergency SQL for the
  latter, since v1 provides no supported command for it.
- `docs/COMPATIBILITY_MATRIX.md` (new): tested-version and feature-support summary,
  distinct from `N8N_COMPATIBILITY.md`'s empirical deep-dive, plus the procedure for
  extending it to a new n8n version.
- `examples/registry/synthetic_test_workflow.json` (new): an importable n8n workflow
  (webhook → validate → route → process/respond, with the `n8n_operator` response
  envelope) — the same structure the phase 4 compatibility spike used, packaged for
  reuse rather than re-derived from a sanitized test fixture.
- `examples/mcp-clients/` (new): ready-to-copy Claude Desktop (stdio) and Streamable
  HTTP configs, both verified against a real build of this package during release
  testing — a full MCP session, not just "the process starts," over each transport.
- `scripts/demo.sh` (new): a five-minute, no-n8n-required walkthrough of discovery,
  validation, and the audit trail against a scratch database.
- `SECURITY.md`, `CONTRIBUTING.md` (new).
- `README.md`, this changelog: brought current with v1's actual shipped state (the
  README had been stale since phase 1, still describing "no registry, MCP tool, or
  n8n integration implemented yet").

### Added — phase 8: operator surface

Implements BUILD_PLAN section 12 phase 8. 51 new tests (926 total), 93% coverage
overall; `core/` (96%) and `registry/` (94–98%) remain above the 90% gate.

- `cli operations list | show | cancel` (`expire` already existed) — a `rich`-table or
  `--json` history, one operation's full detail, and confirm-then-withdraw.
- `cli audit verify | export` (new): `verify` walks the full hash chain and reports
  the first break by sequence number, exiting the new code `2` (distinct from `1`, a
  general/usage error). `export`
  (`core.service.export_audit_record`) produces the full audit log, every operation's
  transitions/actor/timestamps, and the registry snapshots those operations were
  governed against — redacting arguments at the export boundary exactly like
  `get_operation`, and never including `approvals` table content (including the
  token hash) at all.
- `logging_setup.py` (new, greenfield): structured JSON logs on the `n8n_operator`
  logger namespace, a process-wide additive secret-scrub list, and a correlation ID
  bound per CLI invocation and per Streamable HTTP request.
- `cli health`: `get_instance_health` from the command line.

### Added — phase 7: execution and debugging

Implements BUILD_PLAN section 12 phase 7 — the highest-risk boundary. 40 new tests
(875 total), 93% coverage overall.

- `execute_operation` extended with the full pre-burn verification chain: handle/
  operation-ID equality, lazy expiry, environment binding, an argument-fingerprint
  re-verification, the registry's own current-snapshot drift check, then a *live*
  re-check against n8n, and finally `max_concurrent` — the handle is burned *before*
  the concurrency count is read (SQLite is single-writer; the burn is what makes a
  caller's transaction acquire the write lock a stale-count race would otherwise slip
  through).
- `core.service.dispatch_operation` (new): the one function that manages its own
  transactions, sandwiching the real dispatch call between two — a database
  transaction is never held open across a network call.
- Outcome mapping is conservative by construction: confirmed 2xx → `SUCCEEDED`,
  confirmed non-2xx → `FAILED`, timeout/lost response/unparseable body → `UNKNOWN`. A
  malformed-but-parseable correlation envelope does not demote a real success/error to
  `UNKNOWN` — a pre-existing phase 4 test had this backwards and was corrected
  alongside the fix in `n8n/client.py::dispatch_webhook`.
- `n8n/client.py::get_execution_node_trace` (new): the one deliberate exception to
  "never fetch `includeData=true`," reading only five named scalar fields per node so
  it can never forward a node's raw payload.
- Arguments are now stored **raw** at rest (previously redacted, which made dispatch
  and fingerprint re-verification structurally impossible); redaction moved to the
  read boundary (`get_operation`, the approval-decision context).

### Added — phase 6: approval

Implements BUILD_PLAN section 12 phase 6 (ADR-010). 38 new tests (835 total), 94%
coverage overall.

- Approval token service (`core/handles.py`): 256-bit random token, sha256 hash at
  rest, single-use, TTL-bounded, bound to operation ID, principal, argument
  fingerprint, registry snapshot, and definition hash.
- `cli operations approve | reject | expire | approval-status` — the canonical v1
  approval channel, rendering the full decision surface before confirming.
- `approval/app.py` + `approval/routes.py`: a loopback-only FastAPI app, CSRF-
  protected, no token ever in a log line, safe cache headers, no framing.
- Lazy transactional expiry made authoritative everywhere an operation is read or
  acted on, plus a best-effort sweeper and `operations expire` for audit-timeline
  fidelity.
- **Concurrency fix found while building this phase's own tests**: every state
  transition now catches a lost compare-and-set race and re-validates against the
  row's current state, rather than propagating a raw `OptimisticLockError`.

### Added — phase 5: MCP adapter

Implements BUILD_PLAN section 12 phase 5. 92 new tests (797 total), 95% coverage
overall.

- All 12 v1 MCP tools and both v1 resources, over stdio and Streamable HTTP, with
  identical schemas across both transports (AC-23) — verified by a cross-transport
  contract test.
- Response-shaping allowlists on every tool result; a property test asserts no
  configured secret or n8n identifier ever appears in any result (AC-18).
- Streamable HTTP transport security: loopback by default; a non-loopback bind
  requires a bearer token **and** an Origin allowlist, or startup fails (boundary B9,
  AC-20).
- `cli serve stdio | serve http`.

### Added — phase 4: n8n integration

Implements BUILD_PLAN section 12 phase 4 (ADR-005/006/008/009). Includes a live
empirical spike against a real, local-only n8n 2.35.7 instance — see
`docs/N8N_COMPATIBILITY.md` for the full record.

- `n8n/client.py`: httpx client with explicit connect/read timeouts, no retry logic
  anywhere (statically enforced by a grep-based contract test).
- `n8n/canonicalization.py`: versioned, evidence-driven definition hashing — every
  field included by default; phase 4 ships with an **empty** exclusion allowlist,
  since every candidate field tested was found behaviorally significant.
- `n8n/preflight.py`: liveness, active-state, drift, and credential-binding checks,
  including the non-blocking `warn`/`unverifiable` statuses ADR-009 introduces.
- Dispatch correlation via an opt-in response envelope (`trigger.correlation:
  response_envelope`) carrying the n8n execution ID.

### Added — phase 3: core domain

Implements BUILD_PLAN section 12 phase 3.

- `core/service.py`: the full operation lifecycle — twelve states, fifteen
  transitions — as plain functions over domain types, with no dependency on any
  adapter (ADR-001).
- `core/handles.py` (ADR-003): server-minted, single-use operation handles bound to
  principal, workflow, and argument fingerprint.
- `core/idempotency.py`: canonical-JSON argument fingerprints and namespace-scoped
  idempotency (ADR-011).
- `core/redaction.py`: the output redaction engine (`output.redact`, `max_bytes`).
- The hash-chained audit log (`audit/chain.py`, `audit/writer.py`) and the
  append-only `operation_events` trail, written atomically with every transition.

### Added — phase 2: workflow registry

Implements BUILD_PLAN section 12 phase 2 (ADR-002).

- `registry/schema.py` + `registry/loader.py`: the full YAML registry schema, ten
  load-time validation rules (R1–R12 by the time later phases finished), canonical
  content hashing, and immutable snapshotting.
- `registry/validation.py`: caller-argument validation against each workflow's
  declared JSON Schema.
- `cli registry validate | list | show | hash | reload`.

### Added — phase 1: configuration and storage foundation

Implements BUILD_PLAN section 12 phase 1. Does not implement registry behavior, MCP
tools, n8n HTTP calls, approval routes, or workflow execution — those remain later
phases.

- `config.py`: Pydantic v2 `Settings` (`N8N_OPERATOR_` prefix), startup validation for
  every field in ARCHITECTURE.md section 7, `env:`/`keyring:` secret indirection for
  `n8n_api_key` mirroring the registry's `trigger.secret_ref` scheme, and
  `resolve_database_url()` — a database-URL resolver independent of the rest of
  `Settings`, so schema management never requires `N8N_BASE_URL`/`N8N_API_KEY`.
- `errors.py`: the complete 24-code error taxonomy from MCP_TOOLS.md section 4, split
  into `DomainError` / `AuthorizationError` / `ProviderError` / `ConfigurationError` /
  `StorageError`, each carrying a stable `code`, `retryable`, and `remediation` matching
  the documented "model's correct next move" verbatim. `to_dict()` scrubs any
  accidentally-included secret (duck-typed on `get_secret_value`) before serialization.
- `storage/models.py`: the complete v1 schema (BUILD_PLAN section 8.1) — 8 tables,
  portable per ADR-004 rules D1–D10. `UTCDateTime`, a `TypeDecorator` guaranteeing
  timestamps survive a round trip as UTC-aware — added after integration testing showed
  that bare `DateTime(timezone=True)` alone does not on SQLite (the driver returns a
  naive datetime on read, discarding tzinfo that was correctly attached on write).
- Alembic initialized; migration `0001_initial` creates the full schema. Verified:
  empty-database upgrade to head, downgrade/upgrade round trip, and autogenerate against
  the ORM metadata producing an empty diff (AC-24).
- `storage/session.py`: engine/session lifecycle with `PRAGMA foreign_keys=ON`, WAL mode,
  and a busy timeout set at connection setup only (ADR-004 D9). `storage/repository.py`:
  typed repositories per table, the compare-and-set primitives later phases build handle
  burning on (`compare_and_set_state`, `burn_handle`), and no state-machine policy.
- `cli db init | migrate | status`, driving Alembic programmatically against a `Config`
  built without reading `alembic.ini` at runtime, so behavior is identical from a source
  checkout or an installed package.
- Tests: 319 passing — configuration validation and secret redaction, migration
  round-trip and autogenerate-empty, repository CRUD, transaction rollback, idempotency-
  namespace uniqueness, SQLite foreign-key enforcement, a portable-SQL contract (ADR-004
  D1–D10, AST-based to avoid false positives against this codebase's own docstrings), an
  import-graph layering contract, and CLI end-to-end tests via `typer.testing.CliRunner`.
  96% coverage on the modules this phase implements (100% on `storage/models.py`,
  `storage/repository.py`, `errors.py`).

### Added — phase 0: architecture and bootstrap

- Product definition, v1/v2/v3 outcomes, and exact feature boundaries
  (`docs/BUILD_PLAN.md`).
- Operation state machine: twelve states, fifteen transitions, eight invariants.
- Workflow registry schema with ten load-time validation rules.
- MCP tool inventory: 12 tools in v1, 20 in v2, 28 in v3.
- Storage model for SQLite (v1) and PostgreSQL (v2).
- Security boundaries B1–B11 and a full STRIDE threat model with LLM-specific threats.
- Test strategy, acceptance criteria AC-01–AC-25, and a per-phase progress checklist.
- Seven architecture decision records (ADR-001 … ADR-007).
- Python 3.12 / uv / src-layout package skeleton with dependencies pinned:
  MCP Python SDK v2, Pydantic v2, FastAPI, SQLAlchemy 2, Alembic, httpx, Typer,
  pytest, Hypothesis.
- Documentation consistency checker, run in CI and as a contract test.
- Annotated example workflow registry.

### Added — phase 0.1: architecture-decision closure

Closes the unresolved architecture decisions identified at the end of phase 0, before any
Phase 1 implementation. No product functionality is implemented.

- **ADR-008** conservative workflow-definition canonicalization: inclusion by default,
  exclusion only on empirical evidence, an explicit versioned allowlist, sanitized
  fixtures, and rules CAN-01–CAN-07. Phase 4 ships with an empty exclusion allowlist.
- **ADR-009** dispatch correlation and indeterminate outcomes: a timeout is never inferred
  to be a non-event; an opt-in response envelope carries the n8n execution ID; workflows
  without correlation stay executable with reduced reconciliation, reported by preflight;
  credential checks report bindings, never validity.
- **ADR-010** approval delivery and expiry: the CLI is the canonical v1 approval channel
  and the localhost page is convenience; remote callers never receive an unreachable
  loopback URL; lazy transactional expiry is authoritative, with a best-effort sweeper and
  a new `operations expire` maintenance command.
- **ADR-011** core argument limits and idempotency namespaces: a core-enforced canonical
  argument size cap applied before persistence, and idempotency namespaced by
  principal + environment + workflow + key.
- **ADR-012** governed retry and audit anchoring: v2 retries create a new operation with
  everything recalculated and no approval reuse; the `AuditAnchor` interface with a signed
  local anchor file and an authenticated HTTPS webhook as the first implementations.

### Changed

- Invariants extended to I1–I12; boundary controls to B1–B13; registry rules to R1–R12;
  acceptance criteria to AC-01–AC-33.
- Registry gains `trigger.correlation` and `limits.max_argument_bytes`.
- Preflight gains the non-blocking `warn` and `unverifiable` statuses and the check codes
  `CREDENTIAL_VALIDITY_UNVERIFIED`, `NO_EXECUTION_CORRELATION`, and `UNATTENDED_EXECUTION`.
- `prepare_operation` returns `approval_required` and `approval_instructions`;
  `approval_url` is now gated on caller locality.
- Threat T-12 (argument-payload disk exhaustion) reclassified `partial` → `mitigated`.
  New threats T-38–T-41 and L-08; new residual risks RR-8 and RR-9.
- Doc-consistency checker extended with checks D10 (canonicalization rules), D11 (closed
  error taxonomy), and D12 (ADR structure and non-orphanhood).
- New configuration: `N8N_OPERATOR_MAX_ARGUMENT_BYTES`,
  `N8N_OPERATOR_APPROVAL_URL_EXPOSURE`.

### Superseded

- Error code `IDEMPOTENCY_KEY_CONFLICT` → **`IDEMPOTENCY_CONFLICT`** (ADR-011). The
  conflict is between requests within a namespace, not between keys. Enforced by check D11.

Nothing in this release implements product functionality.
