"""Move final_design_count from workflow_runs to run_metrics.

Revision ID: f91c8ab4d302
Revises: e52a8c9d4f10
Create Date: 2026-02-18 21:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "f91c8ab4d302"
down_revision = "e52a8c9d4f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE run_metrics ADD COLUMN IF NOT EXISTS final_design_count BIGINT")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'workflow_runs'
                  AND column_name = 'final_design_count'
            ) THEN
                INSERT INTO run_metrics (run_id, final_design_count)
                SELECT wr.id, wr.final_design_count
                FROM workflow_runs wr
                WHERE wr.final_design_count IS NOT NULL
                ON CONFLICT (run_id) DO UPDATE
                SET final_design_count = EXCLUDED.final_design_count;

                ALTER TABLE workflow_runs DROP COLUMN IF EXISTS final_design_count;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS final_design_count BIGINT")
    op.execute(
        """
        UPDATE workflow_runs wr
        SET final_design_count = rm.final_design_count
        FROM run_metrics rm
        WHERE rm.run_id = wr.id
          AND rm.final_design_count IS NOT NULL;
        """
    )
    op.execute("ALTER TABLE run_metrics DROP COLUMN IF EXISTS final_design_count")

