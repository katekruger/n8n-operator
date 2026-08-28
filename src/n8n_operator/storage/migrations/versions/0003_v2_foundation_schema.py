"""v2 foundation schema.

Schema-only additions from BUILD_PLAN section 8.3 (v2 stage 00 contract, implemented
stage 01): six new tables (``organizations``, ``organization_memberships``,
``environments``, ``workflow_environment_overlays``, ``notification_deliveries``,
``audit_anchors``) and new nullable columns on three existing tables (``principals``,
``operations``, ``approvals``). No v1 behavior changes — nothing here is read or
written by any v1 code path; see ``storage/models.py``'s per-class docstrings for the
ADR each addition implements.

The three ``ALTER TABLE`` blocks use ``batch_alter_table`` even though only
``approvals`` needs the copy-and-recreate SQLite performs under batch mode for a new
foreign key or unique constraint (SQLite's ``ALTER TABLE`` cannot add a constraint any
other way) — using it uniformly for ``principals``/``operations``/``approvals`` keeps
all three blocks structurally identical rather than two plain-``add_column`` blocks and
one batch block that happen to do the same thing three different ways. On PostgreSQL,
`batch_alter_table` executes the same operations directly with no copy step.

``approvals.quorum_count`` is added ``NOT NULL`` to a table v1 has already been writing
rows into for as long as the process has run — that requires a ``server_default`` for
the backfill of existing rows (neither SQLite nor PostgreSQL will add a ``NOT NULL``
column with no way to fill existing rows), removed again immediately after so the
column's only default going forward is the ORM's own Python-side ``default=1``, exactly
like every other defaulted column in this schema (ADR-004 rule D2's reasoning: a
Python-side default computed once, consistently, rather than a server-side one two
dialects might disagree about). Alembic's default ``compare_metadata`` does not diff
``server_default`` (``compare_server_default`` is not enabled in ``env.py``), so
briefly setting one here does not reappear as drift in AC-24's empty-diff check.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "environments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("n8n_base_url_ref", sa.String(), nullable=False),
        sa.Column("n8n_api_key_ref", sa.String(), nullable=False),
        sa.Column("is_production", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_environments_organization_name"),
    )
    op.create_index("ix_environments_organization_id", "environments", ["organization_id"])
    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("active_organization_id", sa.String(), nullable=True),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("workflow_scope", sa.String(), nullable=False),
        sa.Column("environment_scope", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id", "active_organization_id", name="uq_organization_memberships_active"
        ),
    )
    op.create_index(
        "ix_organization_memberships_organization_id",
        "organization_memberships",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_memberships_principal_id", "organization_memberships", ["principal_id"]
    )
    op.create_table(
        "workflow_environment_overlays",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("environment_id", sa.String(), nullable=False),
        sa.Column("n8n_workflow_id", sa.String(), nullable=True),
        sa.Column("definition_hash", sa.String(), nullable=True),
        sa.Column("trigger_path", sa.String(), nullable=True),
        sa.Column("trigger_secret_ref", sa.String(), nullable=True),
        sa.Column("approval_override", sa.String(), nullable=True),
        sa.Column("limits_override", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "approval_override IS NULL OR approval_override IN ('required')",
            name="ck_workflow_environment_overlays_approval",
        ),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "environment_id", name="uq_workflow_environment_overlays_workflow_env"
        ),
    )
    op.create_index(
        "ix_workflow_environment_overlays_environment_id",
        "workflow_environment_overlays",
        ["environment_id"],
    )
    op.create_index(
        "ix_workflow_environment_overlays_workflow_id",
        "workflow_environment_overlays",
        ["workflow_id"],
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('delivered', 'failed', 'pending')",
            name="ck_notification_deliveries_status",
        ),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_notification_deliveries_principal_id", "notification_deliveries", ["principal_id"]
    )
    op.create_index(
        "ix_notification_deliveries_subject_id", "notification_deliveries", ["subject_id"]
    )
    op.create_table(
        "audit_anchors",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("covers_through_seq", sa.Integer(), nullable=False),
        sa.Column("entry_hash", sa.String(), nullable=False),
        sa.Column("implementation", sa.String(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publish_failed", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "implementation IN ('local_file', 'https_webhook')",
            name="ck_audit_anchors_implementation",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_anchors_covers_through_seq", "audit_anchors", ["covers_through_seq"])

    with op.batch_alter_table("principals") as batch_op:
        batch_op.add_column(sa.Column("external_issuer", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("operations") as batch_op:
        batch_op.add_column(sa.Column("organization_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("environment_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("approval_policy_snapshot", sa.JSON(), nullable=True))
        batch_op.create_index("ix_operations_organization_id", ["organization_id"])
        batch_op.create_index("ix_operations_environment_id", ["environment_id"])
        batch_op.create_foreign_key(
            "fk_operations_organization_id", "organizations", ["organization_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_operations_environment_id", "environments", ["environment_id"], ["id"]
        )

    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(
            sa.Column("quorum_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_unique_constraint(
            "uq_approvals_operation_decided_by", ["operation_id", "decided_by"]
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.alter_column("quorum_count", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint("uq_approvals_operation_decided_by", type_="unique")
        batch_op.drop_column("quorum_count")

    with op.batch_alter_table("operations") as batch_op:
        batch_op.drop_constraint("fk_operations_environment_id", type_="foreignkey")
        batch_op.drop_constraint("fk_operations_organization_id", type_="foreignkey")
        batch_op.drop_index("ix_operations_environment_id")
        batch_op.drop_index("ix_operations_organization_id")
        batch_op.drop_column("approval_policy_snapshot")
        batch_op.drop_column("environment_id")
        batch_op.drop_column("organization_id")

    with op.batch_alter_table("principals") as batch_op:
        batch_op.drop_column("disabled_at")
        batch_op.drop_column("external_issuer")

    op.drop_index("ix_audit_anchors_covers_through_seq", table_name="audit_anchors")
    op.drop_table("audit_anchors")
    op.drop_index("ix_notification_deliveries_subject_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_principal_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index(
        "ix_workflow_environment_overlays_workflow_id", table_name="workflow_environment_overlays"
    )
    op.drop_index(
        "ix_workflow_environment_overlays_environment_id",
        table_name="workflow_environment_overlays",
    )
    op.drop_table("workflow_environment_overlays")
    op.drop_index("ix_organization_memberships_principal_id", table_name="organization_memberships")
    op.drop_index(
        "ix_organization_memberships_organization_id", table_name="organization_memberships"
    )
    op.drop_table("organization_memberships")
    op.drop_index("ix_environments_organization_id", table_name="environments")
    op.drop_table("environments")
    op.drop_table("organizations")
