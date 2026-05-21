"""add_launch_ip_to_workflow_runs"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '91b54c7a79c5'
down_revision = 'c8d9e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("launch_ip", postgresql.INET(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "launch_ip")
