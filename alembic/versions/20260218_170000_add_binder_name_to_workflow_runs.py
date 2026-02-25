"""Add binder_name to workflow_runs.

Revision ID: c17a9e4d5b2e
Revises: b0f8c3f4a2d1
Create Date: 2026-02-18 17:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "c17a9e4d5b2e"
down_revision = "b0f8c3f4a2d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS binder_name TEXT")
    op.execute(
        """
        UPDATE workflow_runs
        SET binder_name = sample_id
        WHERE binder_name IS NULL
          AND sample_id IS NOT NULL
          AND btrim(sample_id) <> '';
        """
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "binder_name")
