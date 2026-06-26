"""drop seqera_dataset_id from workflow_runs

Revision ID: a1b2c3d4e5f6
Revises: 8117c4e6e36e
Create Date: 2026-06-26 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '8117c4e6e36e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('workflow_runs', 'seqera_dataset_id')


def downgrade() -> None:
    import sqlalchemy as sa
    op.add_column('workflow_runs', sa.Column('seqera_dataset_id', sa.Text(), nullable=True))
