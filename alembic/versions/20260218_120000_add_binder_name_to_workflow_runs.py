"""add_binder_name_to_workflow_runs

Revision ID: b8f4d9e21c10
Revises: acd29f674da4
Create Date: 2026-02-18 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b8f4d9e21c10"
down_revision = "acd29f674da4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("binder_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "binder_name")
