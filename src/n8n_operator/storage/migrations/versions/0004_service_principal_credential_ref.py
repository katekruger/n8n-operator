"""Service principal credential ref and the external-identity uniqueness constraint.

Two additions from BUILD_PLAN section 8.3/stage 02 to ``principals`` (schema first
declared stage 00/01, this migration lands the last two columns/constraints stage 02
needs): ``credential_ref`` (an ``env:``/``keyring:`` reference, never a literal secret
— ADR-013 section 3) for ``kind='service'`` principals, and
``uq_principals_external_identity`` — a plain, non-partial unique constraint on
``(external_issuer, external_subject)`` enforcing ADR-014's "identity is the pair,
never `sub` alone" at the database layer, not only in application logic. Two rows both
carrying ``NULL`` in both columns (every non-``user`` principal, including all of v1's
`local` rows) never collide under ordinary SQL NULL-uniqueness semantics — the same
portable rule the idempotency-namespace constraint already relies on (ADR-004 D4).

``batch_alter_table`` because SQLite cannot ``ALTER TABLE ADD CONSTRAINT`` outside batch
mode (the same reason migration 0003 uses it for every existing-table alteration).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("principals") as batch_op:
        batch_op.add_column(sa.Column("credential_ref", sa.String(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_principals_external_identity", ["external_issuer", "external_subject"]
        )


def downgrade() -> None:
    with op.batch_alter_table("principals") as batch_op:
        batch_op.drop_constraint("uq_principals_external_identity", type_="unique")
        batch_op.drop_column("credential_ref")
