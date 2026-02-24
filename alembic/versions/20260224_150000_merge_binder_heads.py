"""Merge Alembic heads after binder_name/sample_id refactors.

Revision ID: a4b5c6d7e8f9
Revises: b8f4d9e21c10, d3e9d6428af1
Create Date: 2026-02-24 15:00:00.000000
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "a4b5c6d7e8f9"
down_revision = ("b8f4d9e21c10", "d3e9d6428af1")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge-only revision."""


def downgrade() -> None:
    """Merge-only revision."""

