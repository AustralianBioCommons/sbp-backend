"""Add final_design_count to workflow_runs.

Revision ID: e52a8c9d4f10
Revises: c17a9e4d5b2e
Create Date: 2026-02-18 19:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "e52a8c9d4f10"
down_revision = "c17a9e4d5b2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS final_design_count BIGINT")


def downgrade() -> None:
    op.drop_column("workflow_runs", "final_design_count")

