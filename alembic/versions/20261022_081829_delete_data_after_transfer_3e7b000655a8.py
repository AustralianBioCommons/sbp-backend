"""delete-data-after-transfer"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3e7b000655a8'
down_revision = '2fdcf073982a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('data_transfers', sa.Column('cleanup_status', sa.String(length=20), nullable=True))
    op.add_column('data_transfers', sa.Column('cleanup_transfer_id', sa.Text(), nullable=True))
    op.add_column('data_transfers', sa.Column('cleanup_error_message', sa.Text(), nullable=True))
    op.add_column(
        'data_transfers',
        sa.Column('cleanup_attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    op.execute(
        """
        UPDATE data_transfers
        SET cleanup_status = 'pending'
        WHERE direction = 'output' AND status = 'completed'
        """
    )


def downgrade() -> None:
    op.drop_column('data_transfers', 'cleanup_attempts')
    op.drop_column('data_transfers', 'cleanup_error_message')
    op.drop_column('data_transfers', 'cleanup_transfer_id')
    op.drop_column('data_transfers', 'cleanup_status')
