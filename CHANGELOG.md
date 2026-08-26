# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
