"""replace data transfer provider metadata with submission id"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '70ac6b86efb4'
down_revision = '3ddc0b088b8b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # provider_metadata only ever stored {"submission_id": ...} (see
    # submit_pending_transfer in app/services/globus_transfer.py). transfer_id
    # now doubles for this: it holds the submission id until submission
    # succeeds, then gets overwritten with the real Globus task id - so there's
    # no need for a separate column. Backfill only rows that haven't submitted
    # yet (transfer_id still NULL); rows past that point already have their
    # real task id in transfer_id and must not be overwritten with a stale
    # submission id.
    op.execute(
        "UPDATE data_transfers SET transfer_id = provider_metadata->>'submission_id' "
        "WHERE transfer_id IS NULL AND provider_metadata IS NOT NULL"
    )
    op.drop_column('data_transfers', 'provider_metadata')


def downgrade() -> None:
    op.add_column('data_transfers', sa.Column('provider_metadata', sa.JSON(), nullable=True))
    # Only restorable for rows still pending submission - once submitted,
    # transfer_id holds the real task id, not a submission id, and the two
    # can no longer be told apart.
    op.execute(
        "UPDATE data_transfers SET provider_metadata = "
        "jsonb_build_object('submission_id', transfer_id) "
        "WHERE status = 'pending' AND transfer_id IS NOT NULL"
    )
    op.execute("UPDATE data_transfers SET transfer_id = NULL WHERE status = 'pending'")
