"""add sample_id to workflow_runs and final_design_count to run_metrics

Revision ID: c13f1d0d8c3a
Revises: b8f4d9e21c10
Create Date: 2026-02-23 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c13f1d0d8c3a"
down_revision = "b8f4d9e21c10"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("workflow_runs", "sample_id"):
        op.add_column("workflow_runs", sa.Column("sample_id", sa.Text(), nullable=True))
    if not _has_column("run_metrics", "final_design_count"):
        op.add_column("run_metrics", sa.Column("final_design_count", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    if _has_column("run_metrics", "final_design_count"):
        op.drop_column("run_metrics", "final_design_count")
    if _has_column("workflow_runs", "sample_id"):
        op.drop_column("workflow_runs", "sample_id")
