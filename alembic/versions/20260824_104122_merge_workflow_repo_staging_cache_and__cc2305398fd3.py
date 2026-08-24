"""Merge Alembic heads: workflow repo staging cache and recursive data transfer.

Revision ID: cc2305398fd3
Revises: b1f4c9a7e021, 4ac790a42b41
Create Date: 2026-08-24 10:41:22.000000
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "cc2305398fd3"
down_revision = ("b1f4c9a7e021", "4ac790a42b41")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge-only revision."""


def downgrade() -> None:
    """Merge-only revision."""
