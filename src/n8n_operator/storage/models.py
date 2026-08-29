"""SQLAlchemy 2.0 ORM models (typed declarative style).

The complete v1 schema from BUILD_PLAN section 8.1: principals, registry_snapshots,
workflow_bindings, operations, operation_events, approvals, execution_results,
audit_log. (``alembic_version`` is managed by Alembic itself and has no model here.)

Only portable constructs are used, per ADR-004's binding rules D1-D10:

* every primary key is a ULID string, generated client-side with a Python-side
  ``default=`` — except ``audit_log.seq``, the one deliberate exception, documented on
  :class:`AuditLogEntry` (D1);
* every timestamp is ``DateTime(timezone=True)``, always populated by a Python-side
  ``default=`` callable (:func:`utc_now`), never a server default — SQLite and
  PostgreSQL disagree about what ``func.now()`` produces, and a naive value compares
  wrongly across the v2 migration (D2);
* every structured column is the generic ``JSON`` type, never a dialect-specific
  ``JSONB`` (D3);
* enum-like text columns carry a plain ``CHECK (col IN (...))`` constraint rather than a
  native database enum type (D10);
* the idempotency-namespace uniqueness constraint (ADR-011) is a **plain** multi-column
  ``UniqueConstraint`` with no ``WHERE`` clause — not a partial index. Standard SQL
  treats ``NULL`` as distinct from every other ``NULL`` for uniqueness purposes in both
  SQLite and PostgreSQL, so two rows sharing a namespace with no ``idempotency_key`` set
  never collide, without needing an engine-specific partial/filtered index (D4).

No ``JSONB``, no partial index, no server-side UUID generator, and no other
engine-specific SQL appears in this module or in the migration derived from it.

Uniqueness is enforced by these database constraints, not by application checks
(ADR-004 D8). All access goes through this ORM or hand-written Core statements in
``storage/repository.py`` — no raw SQL string appears outside a migration (D5).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, Boolean, DateTime, Integer, String, TypeDecorator
from ulid import ULID

# --------------------------------------------------------------------------------------
# Shared vocabulary — mirrors BUILD_PLAN sections 5.1 and 5.2 exactly. A doc-consistency
# style contract test cross-checks this list against the state and transition tables in
# BUILD_PLAN.md, so the two cannot silently drift.
# --------------------------------------------------------------------------------------

STATES: tuple[str, ...] = (
    "PREPARING",
    "INVALID",
    "BLOCKED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELED",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
)

TRANSITIONS: tuple[str, ...] = tuple(f"T{n:02d}" for n in range(1, 16))  # T01-T15

GENESIS_HASH = "0" * 64
"""``audit_log.prev_hash`` for the first entry in the chain (BUILD_PLAN section 8.1)."""


def utc_now() -> datetime:
    """The one place a Python-side timestamp default is computed (ADR-004 rule D2).

    Every timestamp default in this module calls this instead of relying on a database
    server default: SQLite and PostgreSQL differ in what a server-side ``now()`` produces
    and in what timezone (if any) it carries, and a naive value compares wrongly once the
    v2 migration moves the same schema onto PostgreSQL. Computing it in Python, once, at
    insert time keeps every row unambiguously UTC and dialect-independent.
    """
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware ``DateTime`` that round-trips as UTC on every dialect.

    ``DateTime(timezone=True)`` alone does not actually satisfy ADR-004 rule D2 on
    SQLite: SQLite has no native datetime type, and the sqlite3 driver returns a value
    that was written with UTC tzinfo attached as a **naive** datetime on read back
    (empirically confirmed — inserting a UTC-aware value and reading it back yields
    ``tzinfo=None``). PostgreSQL's real ``timestamptz`` does not have this problem, so
    without this type decorator the same schema would behave correctly in v2 while being
    silently wrong in v1, which is exactly the kind of dialect-dependent surprise D2
    exists to rule out.

    Every value written through this column must already be timezone-aware (a naive
    value is a caller bug — this module's own :func:`utc_now` never produces one, so a
    naive value reaching here means something upstream did not); it is converted to UTC
    before storage. Every value read back is normalized to UTC, attaching ``tzinfo=UTC``
    if the underlying driver returned it naive (true on SQLite) and converting in place
    if the driver already preserved an offset (true on PostgreSQL) — in both cases the
    caller always receives a real, UTC, timezone-aware ``datetime``.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "a naive datetime was passed to a UTCDateTime column (ADR-004 rule D2 "
                "requires every timestamp to be timezone-aware) — use "
                "n8n_operator.storage.models.utc_now() or an equivalent aware value"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def new_ulid() -> str:
    """A fresh ULID string. The default for every client-generated primary key here."""
    return str(ULID())


class Base(DeclarativeBase):
    """Declarative base for every table in BUILD_PLAN section 8.1."""


def _enum_check(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """A plain, portable ``CHECK (column IN (...))`` — never a native enum type (D10)."""
    literal_list = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({literal_list})", name=name)


def _nullable_enum_check(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """As :func:`_enum_check`, but also permitting ``NULL`` for an optional column."""
    literal_list = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IS NULL OR {column} IN ({literal_list})", name=name)


class Principal(Base):
    """Who acted (BUILD_PLAN 8.1). v1 holds exactly one row, ``kind='local'``.

    ``external_issuer``/``disabled_at`` are v2-foundational (BUILD_PLAN section 8.3,
    stage 01), implemented stage 02. Identity in v2 is the pair ``(external_issuer,
    external_subject)``, never ``external_subject`` alone (ADR-014) — enforced here by
    ``uq_principals_external_identity``, a plain (non-partial) unique constraint: two
    rows both carrying ``NULL`` (every ``local``/``service`` principal) never collide,
    the same portable NULL-uniqueness rule the idempotency-namespace constraint already
    documents (ADR-004 rule D4). ``disabled_at`` is checked live on every request that
    resolves identity (ADR-014 section 4, stage 02) — no cache, ever.

    ``credential_ref`` is stage 02's addition for ``kind='service'``: an ``env:NAME`` /
    ``keyring:SERVICE/ACCOUNT`` reference (ADR-006's indirection, ADR-013 section 3),
    never a literal secret or a hash Operator itself mints — the same pattern
    ``config.n8n_api_key`` and ``environments.n8n_api_key_ref`` already use. Rotating a
    service principal's credential is repointing this reference after the new secret
    value has been set at the referenced location, not an Operator-side secret-issuance
    flow.
    """

    __tablename__ = "principals"
    __table_args__ = (
        _enum_check("kind", ("local", "user", "service"), name="ck_principals_kind"),
        UniqueConstraint(
            "external_issuer", "external_subject", name="uq_principals_external_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    external_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    external_issuer: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class RegistrySnapshot(Base):
    """One canonicalized, hashed load of the registry (BUILD_PLAN section 6.7)."""

    __tablename__ = "registry_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    content_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class WorkflowBinding(Base):
    """One resolved registry entry within one snapshot (BUILD_PLAN 8.1)."""

    __tablename__ = "workflow_bindings"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "workflow_id", name="uq_workflow_bindings_snapshot_workflow"
        ),
        _enum_check(
            "side_effects",
            ("read_only", "external_write", "irreversible"),
            name="ck_workflow_bindings_side_effects",
        ),
        _enum_check(
            "approval_policy", ("none", "required"), name="ck_workflow_bindings_approval_policy"
        ),
        Index("ix_workflow_bindings_snapshot_id", "snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("registry_snapshots.id"), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    n8n_workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String, nullable=False)
    side_effects: Mapped[str] = mapped_column(String, nullable=False)
    approval_policy: Mapped[str] = mapped_column(String, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Operation(Base):
    """The governance record — the unit BUILD_PLAN section 5 describes (section 8.1).

    ``id`` is the ``op_<ULID>`` operation handle (ADR-003) and is deliberately **not**
    auto-generated here the way every other table's primary key is: minting it is
    ``core/handles.py``'s job (phase 3), and giving the storage layer that responsibility
    would blur exactly the boundary ADR-001 draws between the portable core and the
    infrastructure it depends on. A caller must supply ``id`` explicitly.

    The idempotency-namespace unique constraint below is the storage-level enforcement of
    invariant I8 (ADR-011): it is a plain constraint, not a partial index, and relies on
    ordinary SQL ``NULL``-uniqueness semantics — see the module docstring. It was
    deliberately **not** widened to include ``parent_operation_id`` in stage 06: standard
    SQL exempts a row from a composite unique constraint entirely as soon as any one of
    its columns is ``NULL`` (the same rule this class's own docstring already relies on
    for ``idempotency_key`` itself), so adding a *nullable* ``parent_operation_id`` would
    have silently stopped enforcing uniqueness for every ordinary (non-retry,
    ``parent_operation_id IS NULL``) row — the vast majority of them. Instead,
    ``core.service._prepare_or_retry`` folds the parent into the *value* it stores in
    ``idempotency_key`` for a retry (``f"retry:{parent_operation_id}:{key}"``, internal
    only, never echoed to a caller) — the existing 4-column constraint, unchanged since
    ADR-011, keeps working for both cases without a schema change.
    """

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "environment",
            "workflow_id",
            "idempotency_key",
            name="uq_operations_idempotency_namespace",
        ),
        _enum_check("state", STATES, name="ck_operations_state"),
        Index("ix_operations_workflow_id", "workflow_id"),
        Index("ix_operations_state", "state"),
        Index("ix_operations_principal_id", "principal_id"),
        Index("ix_operations_snapshot_id", "snapshot_id"),
        Index("ix_operations_parent_operation_id", "parent_operation_id"),
        Index("ix_operations_organization_id", "organization_id"),
        Index("ix_operations_environment_id", "environment_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("registry_snapshots.id"), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    argument_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    argument_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    handle_burned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    execution_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    n8n_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.id"), nullable=True
    )
    # v2-foundational (BUILD_PLAN section 8.3, stage 01): schema only. BUILD_PLAN
    # describes organization_id as "not null in v2" at the application level once
    # stage 02 (organizations) exists to populate it; the column itself must stay
    # nullable here, or every v1 write path (which knows nothing of organizations)
    # would fail its NOT NULL constraint today.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id"), nullable=True)
    approval_policy_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class OperationEvent(Base):
    """Append-only transition log (BUILD_PLAN 8.1). Never updated, never deleted."""

    __tablename__ = "operation_events"
    __table_args__ = (
        _enum_check("transition", TRANSITIONS, name="ck_operation_events_transition"),
        Index("ix_operation_events_operation_id", "operation_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    transition: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class Approval(Base):
    """Out-of-band human decisions (BUILD_PLAN 8.1). The token itself is never stored —
    only its hash, computed by the caller before this row is written.

    ``binding_hash`` (phase 6, ADR-010) is a sha256 over the operation's own
    ``(id, principal_id, argument_fingerprint, snapshot_id, definition_hash)`` at mint
    time (``core/handles.py``'s ``compute_approval_binding``) — none of those columns
    is ever updated after an operation is created, so this is a structural invariant
    that already holds; storing and re-checking it at redemption is defense in depth
    against a future regression (e.g. a v2 retry path that reuses a row) rather than a
    condition reachable through any v1 code path today.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        _nullable_enum_check("decision", ("approved", "rejected"), name="ck_approvals_decision"),
        Index("ix_approvals_operation_id", "operation_id"),
        # v2-foundational (BUILD_PLAN section 8.3, stage 01): once team-approval quorum
        # writes multiple decision rows per operation (stage 05), no two rows may carry
        # the same (operation_id, decided_by) pair (ADR-017 section 3). Safe to add now:
        # v1 writes exactly one row per operation via ApprovalRepository.create, and
        # decided_by starts NULL (plain SQL NULL-uniqueness means it never collides,
        # the same rule documented on the idempotency-namespace constraint above) and
        # is set exactly once by record_decision — never a second insert.
        UniqueConstraint("operation_id", "decided_by", name="uq_approvals_operation_decided_by"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    binding_hash: Mapped[str] = mapped_column(String, nullable=False)
    quorum_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Stage 05 (ADR-017): which eligible approver this *pending, undecided* row
    # belongs to — `NULL` for a v1 shared token (unchanged) and for a v2 decision
    # cast directly through the CLI without a prior `request_approval` minting it.
    # Once decided, `decided_by` is the authoritative record; `assigned_to` only ever
    # matters before that, to let the web channel's per-approver link know who it
    # decides as without a login system (`core.service.resolve_approval_token`).
    assigned_to: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    client_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)


class ExecutionResult(Base):
    """What n8n returned (BUILD_PLAN 8.1). One row per operation — v1 never retries, so
    there is never a second result to reconcile against the first."""

    __tablename__ = "execution_results"
    __table_args__ = (
        _enum_check(
            "status", ("success", "error", "indeterminate"), name="ck_execution_results_status"
        ),
    )

    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    n8n_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    node_trace: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AuditLogEntry(Base):
    """Append-only, hash-chained (BUILD_PLAN section 9.4).

    ``seq`` is the single deliberate exception to ADR-004 rule D1 ("primary keys are
    ULID strings, never AUTOINCREMENT or SERIAL"). A hash chain needs strict, gap-free
    monotonic ordering to make "the first break" a well-defined concept; a ULID is only
    *roughly* time-sortable and gives no such guarantee under concurrent inserts. A plain
    database-generated integer identity is itself a fully portable SQLAlchemy construct
    across SQLite and PostgreSQL — this is the schema explicitly calling for that
    construct (BUILD_PLAN 8.1: "seq | integer PK | Monotonic."), not an accidental
    departure from it.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        _enum_check("outcome", ("allowed", "denied", "error"), name="ck_audit_log_outcome"),
        Index("ix_audit_log_occurred_at", "occurred_at"),
        # Stage 06, migration 0006: the read path a reconciliation-evidence query
        # (`operations reconcile list`) needs — there was nothing to query by subject
        # before this stage had any reason to look one up.
        Index("ix_audit_log_subject", "subject_type", "subject_id"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Organization(Base):
    """The v2 tenant / isolation boundary (BUILD_PLAN section 8.3, ADR-013).

    v2-foundational: schema only, stage 01. No v1 code path creates, reads, or
    references a row here — organizations exist starting stage 02.
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class OrganizationMembership(Base):
    """The RBAC grant: which roles a principal holds in an organization, and over what
    workflow/environment scope (BUILD_PLAN section 8.3, ADR-013, ADR-015).

    v2-foundational: schema only, stage 01. ``active_organization_id`` is a portable
    stand-in for "unique on (principal_id, organization_id) while removed_at IS NULL"
    (ADR-004 forbids partial/filtered indexes, rule D4) — the same plain-SQL
    NULL-uniqueness technique the ``operations`` idempotency-namespace constraint
    already relies on (see the module docstring), applied here instead of there.
    Whichever stage-03 code creates or removes a membership is responsible for keeping
    ``active_organization_id`` in lock-step with ``removed_at``: equal to
    ``organization_id`` while the membership is active, ``NULL`` once removed. The
    ``uq_organization_memberships_active`` constraint enforces the *uniqueness* the
    database can guarantee (no two active rows collide); keeping the two columns
    consistent with each other is the same kind of repository-layer discipline
    ``state_version`` already requires from every ``operations`` writer.
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint(
            "principal_id", "active_organization_id", name="uq_organization_memberships_active"
        ),
        Index("ix_organization_memberships_principal_id", "principal_id"),
        Index("ix_organization_memberships_organization_id", "organization_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), nullable=False)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    active_organization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    workflow_scope: Mapped[str] = mapped_column(String, nullable=False)
    environment_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Environment(Base):
    """One named n8n instance within an organization (BUILD_PLAN section 8.3, ADR-016).

    v2-foundational: schema only, stage 01. ``n8n_base_url_ref``/``n8n_api_key_ref``
    carry the same ``env:``/``keyring:`` indirection ``config.py`` already requires for
    ``n8n_api_key`` (ADR-006) and the registry's ``trigger.secret_ref`` (rule R6) —
    never a literal URL or key in this column once stage 04 starts writing it.
    """

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_environments_organization_name"),
        Index("ix_environments_organization_id", "organization_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    n8n_base_url_ref: Mapped[str] = mapped_column(String, nullable=False)
    n8n_api_key_ref: Mapped[str] = mapped_column(String, nullable=False)
    is_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


class WorkflowEnvironmentOverlay(Base):
    """A per-environment override of a registry entry's reachability/strictness — never
    its contract (BUILD_PLAN section 8.3, ADR-016, rules R13-R14).

    v2-foundational: schema only, stage 01. ``workflow_id`` has no foreign key, exactly
    like ``operations.workflow_id`` above — the registry itself is not a queryable table
    with its own primary key, only a plain string this column indexes against.
    """

    __tablename__ = "workflow_environment_overlays"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "environment_id", name="uq_workflow_environment_overlays_workflow_env"
        ),
        _nullable_enum_check(
            "approval_override", ("required",), name="ck_workflow_environment_overlays_approval"
        ),
        Index("ix_workflow_environment_overlays_workflow_id", "workflow_id"),
        Index("ix_workflow_environment_overlays_environment_id", "environment_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), nullable=False)
    n8n_workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    definition_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_path: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_secret_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_override: Mapped[str | None] = mapped_column(String, nullable=True)
    limits_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class NotificationDelivery(Base):
    """One delivery attempt record for the ``NotificationSink`` interface — approval
    routing and alert hooks alike (BUILD_PLAN section 8.3, ADR-018).

    v2-foundational: schema only, stage 01.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        _enum_check(
            "status",
            ("delivered", "failed", "pending"),
            name="ck_notification_deliveries_status",
        ),
        Index("ix_notification_deliveries_subject_id", "subject_id"),
        Index("ix_notification_deliveries_principal_id", "principal_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AuditAnchor(Base):
    """One receipt from ``AuditAnchor.publish`` — content-free, pins chain state only
    (BUILD_PLAN section 8.3, ADR-012 section 2).

    v2-foundational: schema only, stage 01. Implemented starting stage 09.
    """

    __tablename__ = "audit_anchors"
    __table_args__ = (
        _enum_check(
            "implementation",
            ("local_file", "https_webhook"),
            name="ck_audit_anchors_implementation",
        ),
        Index("ix_audit_anchors_covers_through_seq", "covers_through_seq"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_ulid)
    covers_through_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False)
    implementation: Mapped[str] = mapped_column(String, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)
    publish_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


__all__ = [
    "GENESIS_HASH",
    "STATES",
    "TRANSITIONS",
    "Approval",
    "AuditAnchor",
    "AuditLogEntry",
    "Base",
    "Environment",
    "ExecutionResult",
    "NotificationDelivery",
    "Operation",
    "OperationEvent",
    "Organization",
    "OrganizationMembership",
    "Principal",
    "RegistrySnapshot",
    "UTCDateTime",
    "WorkflowBinding",
    "WorkflowEnvironmentOverlay",
    "new_ulid",
    "utc_now",
]
