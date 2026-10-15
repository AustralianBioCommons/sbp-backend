"""run_outputs share data_transfer

A recursive Globus output transfer copies a whole folder, so every file it
delivers now links back to that transfer instead of getting its own
provider="s3" bookkeeping row. Existing outputs are relinked to the Globus
transfer whose destination covers them (most specific first), and the
bookkeeping rows they leave orphaned are deleted.

Downgrade restores the unique constraint by unlinking outputs from Globus
transfers; it does not recreate the deleted bookkeeping rows.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = '2fdcf073982a'
down_revision = '5d3c1e7a9b20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f('uq_run_outputs_data_transfer_id'), 'run_outputs', type_='unique')
    op.execute(
        """
        UPDATE run_outputs ro
        SET data_transfer_id = m.globus_id
        FROM (
            SELECT DISTINCT ON (ro2.run_id, ro2.s3_object_id)
                ro2.run_id, ro2.s3_object_id, dt.id AS globus_id
            FROM run_outputs ro2
            JOIN data_transfers old ON old.id = ro2.data_transfer_id AND old.provider = 's3'
            JOIN s3_objects so ON so.object_key = ro2.s3_object_id
            JOIN data_transfers dt
                ON dt.workflow_run_id = ro2.run_id
                AND dt.provider = 'globus'
                AND dt.direction = 'output'
                AND (
                    dt.destination_location = so."URI"
                    OR (
                        dt.recursive
                        AND left(so."URI", length(rtrim(dt.destination_location, '/')) + 1)
                            = rtrim(dt.destination_location, '/') || '/'
                    )
                )
            ORDER BY ro2.run_id, ro2.s3_object_id, length(dt.destination_location) DESC
        ) m
        WHERE ro.run_id = m.run_id AND ro.s3_object_id = m.s3_object_id
        """
    )
    op.execute(
        """
        DELETE FROM data_transfers dt
        WHERE dt.provider = 's3'
            AND dt.direction = 'output'
            AND NOT EXISTS (SELECT 1 FROM run_outputs ro WHERE ro.data_transfer_id = dt.id)
            AND NOT EXISTS (SELECT 1 FROM run_inputs ri WHERE ri.data_transfer_id = dt.id)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE run_outputs ro
        SET data_transfer_id = NULL
        FROM data_transfers dt
        WHERE dt.id = ro.data_transfer_id AND dt.provider = 'globus'
        """
    )
    op.create_unique_constraint(
        op.f('uq_run_outputs_data_transfer_id'), 'run_outputs', ['data_transfer_id']
    )
