"""Add sample_id to workflow_runs.

Revision ID: b0f8c3f4a2d1
Revises: acd29f674da4
Create Date: 2026-02-18 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b0f8c3f4a2d1"
down_revision = "acd29f674da4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("sample_id", sa.Text(), nullable=True))
    # Backfill from legacy column if it exists in this database.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'workflow_runs'
                  AND column_name = 'binder_name'
            ) THEN
                UPDATE workflow_runs
                SET sample_id = binder_name
                WHERE sample_id IS NULL
                  AND binder_name IS NOT NULL
                  AND btrim(binder_name) <> '';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "sample_id")
