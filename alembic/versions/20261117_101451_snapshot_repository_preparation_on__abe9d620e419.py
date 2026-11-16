"""Capture each queued job's repository mode, checkout, and readiness."""

import sqlalchemy as sa

from alembic import op

revision = "abe9d620e419"
down_revision = "2c5bf512f6a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "queued_jobs",
        sa.Column("repository_transfer", sa.String(20), nullable=False, server_default="native"),
    )
    op.add_column("queued_jobs", sa.Column("repository_location", sa.Text(), nullable=True))
    op.add_column(
        "queued_jobs",
        sa.Column("repository_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("queued_jobs") as batch:
        batch.alter_column(
            "repository_transfer",
            existing_type=sa.String(20),
            existing_nullable=False,
            server_default=None,
        )
        batch.alter_column(
            "repository_ready",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("queued_jobs") as batch:
        batch.drop_column("repository_ready")
        batch.drop_column("repository_location")
        batch.drop_column("repository_transfer")
