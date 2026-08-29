"""An audit-log subject index.

``ix_audit_log_subject`` supports the new ``operations reconcile list`` CLI command
(stage 06): reconciliation evidence is recorded as a plain audit-log annotation
(ADR-009/ADR-012 — never a new table, never a state transition), and the table had no
index on ``(subject_type, subject_id)`` to query by until now.

``operations.parent_operation_id`` itself needs no schema change here — present since
migration 0001, unused until this stage. Nor does the idempotency-namespace unique
constraint: standard SQL exempts a row from a composite unique constraint entirely as
soon as any one of its columns is ``NULL``, so widening it with a *nullable*
``parent_operation_id`` would have silently stopped enforcing uniqueness for every
ordinary (non-retry) row — the vast majority of them. ``core.service._prepare_or_retry``
instead folds a retry's parent into the *value* it stores in ``idempotency_key``
(``f"retry:{parent_operation_id}:{key}"``, internal only, never echoed to a caller) — see
``storage/models.py``'s ``Operation`` docstring.

``batch_alter_table`` for SQLite's copy-and-recreate ``ALTER TABLE`` (ADR-004 rule D9,
the same reason migrations 0002/0003/0005 use it).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.create_index("ix_audit_log_subject", ["subject_type", "subject_id"])


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_index("ix_audit_log_subject")
