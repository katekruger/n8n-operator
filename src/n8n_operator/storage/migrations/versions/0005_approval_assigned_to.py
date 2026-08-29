"""Per-approver assignment for pending approval rows.

Stage 05 (ADR-017): mints one ``Approval`` row per eligible approver instead of one
shared row per operation, so the web approval channel can know *which* eligible
approver a given link decides as without a login system. ``assigned_to`` names that
principal for a pending, undecided row; ``NULL`` for a v1 shared token (unchanged)
and for a v2 decision cast directly through the CLI without a prior
``request_approval`` minting one. Every other piece of stage 05's schema
(``approvals.quorum_count``, the ``(operation_id, decided_by)`` unique constraint,
``operations.approval_policy_snapshot``, the ``notification_deliveries`` table) was
already added schema-only in migration 0003 and needs no further change here.

``batch_alter_table`` for SQLite's copy-and-recreate ``ALTER TABLE`` (ADR-004 rule D9,
the same reason migrations 0003/0004 use it).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("assigned_to", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("assigned_to")
