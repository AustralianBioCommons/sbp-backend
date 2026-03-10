"""Add submitted_form_data to workflow_runs.

Revision ID: c8d9e1f2a3b4
Revises: a4b5c6d7e8f9
Create Date: 2026-03-10 10:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c8d9e1f2a3b4"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("submitted_form_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "submitted_form_data")
