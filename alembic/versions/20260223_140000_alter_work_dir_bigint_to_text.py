"""alter workflow_runs.work_dir from bigint to text for existing DBs

Revision ID: d3e9d6428af1
Revises: f91c8ab4d302
Create Date: 2026-02-23 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "d3e9d6428af1"
down_revision = "f91c8ab4d302"
branch_labels = None
depends_on = None


def _work_dir_is_text() -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = inspector.get_columns("workflow_runs")
    for column in columns:
        if column["name"] == "work_dir":
            return isinstance(column["type"], sa.Text)
    raise RuntimeError("workflow_runs.work_dir column not found")


def upgrade() -> None:
    if _work_dir_is_text():
        return

    op.alter_column(
        "workflow_runs",
        "work_dir",
        existing_type=postgresql.BIGINT(),
        type_=sa.Text(),
        postgresql_using="work_dir::text",
    )


def downgrade() -> None:
    # Intentionally non-reversible: work_dir now stores path strings in production.
    # Converting arbitrary text paths back to bigint is unsafe.
    pass
