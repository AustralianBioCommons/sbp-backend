"""add workflow repo staging cache"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1f4c9a7e021'
down_revision = '70ac6b86efb4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('repo_staged_commit_sha', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('repo_staging_status', sa.String(length=20), nullable=True))
    op.add_column('workflows', sa.Column('repo_gadi_path', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('repo_staging_transfer_id', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('repo_staging_error_message', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('repo_staging_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('workflows', 'repo_staging_updated_at')
    op.drop_column('workflows', 'repo_staging_error_message')
    op.drop_column('workflows', 'repo_staging_transfer_id')
    op.drop_column('workflows', 'repo_gadi_path')
    op.drop_column('workflows', 'repo_staging_status')
    op.drop_column('workflows', 'repo_staged_commit_sha')
