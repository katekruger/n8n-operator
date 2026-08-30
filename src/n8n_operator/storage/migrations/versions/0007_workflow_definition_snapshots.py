"""Workflow definition snapshots.

Stage 07 (ADR-008, MCP_TOOLS.md section 5.6): the registry only ever stores a workflow's
``definition_hash`` — never the definition it was computed from
(``docs/WORKFLOW_REGISTRY.md``'s schema has no raw-definition field) — so there is
nothing to diff a live definition against unless something persists the canonical
structure at the moment a hash is adopted. This table is that persistence: one row per
``(workflow_id, definition_hash)`` pair, captured by ``registry hash --n8n-workflow-id``
(``cli/commands/registry.py``, finally implemented this stage) each time an operator
adopts a new hash. ``diff_workflow_definition`` looks a snapshot up by the registry's
*current* ``definition_hash``; a hash with no captured snapshot (a pre-existing entry,
or one typed in by hand) still gets an honest ``changed``/hash comparison from the tool
— just an empty, clearly-flagged diff instead of an itemized one.

``canonical_definition`` stores exactly ``n8n.canonicalization.canonical_form()``'s own
output — never the full raw n8n response, which carries administrative row metadata
(``id``, ``name``, timestamps, version history) canonicalization already excludes
structurally. Contains no actual secret *values* (n8n's own workflow read never returns
credential secrets, only ID/name bindings) — stored unredacted, consistent with how
operation arguments are already handled (redaction happens at the read boundary, never
at rest).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_definition_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("definition_hash", sa.String(), nullable=False),
        sa.Column("canonical_definition", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id",
            "definition_hash",
            name="uq_workflow_definition_snapshots_workflow_hash",
        ),
    )
    op.create_index(
        "ix_workflow_definition_snapshots_workflow_id",
        "workflow_definition_snapshots",
        ["workflow_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_definition_snapshots_workflow_id",
        table_name="workflow_definition_snapshots",
    )
    op.drop_table("workflow_definition_snapshots")
