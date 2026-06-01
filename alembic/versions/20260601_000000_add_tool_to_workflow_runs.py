"""add tool to workflow_runs"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_tool_workflow_runs'
down_revision = '0405f3482868'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('tool', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'tool')
