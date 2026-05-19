"""Add last_login_ip to app_users.

Revision ID: e7f8a9b0c1d2
Revises: c8d9e1f2a3b4
Create Date: 2026-05-19 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e7f8a9b0c1d2"
down_revision = "c8d9e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("last_login_ip", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_users", "last_login_ip")
