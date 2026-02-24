"""add_binder_name_to_workflow_runs

Revision ID: b8f4d9e21c10
Revises: acd29f674da4
Create Date: 2026-02-18 12:00:00.000000
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b8f4d9e21c10"
down_revision = "acd29f674da4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep this migration idempotent so it can coexist with later binder_name migrations.
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS binder_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS binder_name")
