"""Add tool column to workflow_runs and backfill from submitted_form_data."""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '797ec472e447'
down_revision = '0405f3482868'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('tool', sa.Text(), nullable=True))

    # Rows with a 'mode' key in submitted_form_data are Single Prediction runs.
    # tool = the mode value (e.g. "colabfold", "alphafold2").
    op.execute("""
        UPDATE workflow_runs
        SET
            tool = submitted_form_data::jsonb ->> 'mode',
            workflow_id = (SELECT id FROM workflows WHERE name = 'single-prediction')
        WHERE
            tool IS NULL
            AND submitted_form_data IS NOT NULL
            AND (submitted_form_data::jsonb ? 'mode')
            AND (submitted_form_data::jsonb ->> 'mode') IS NOT NULL
            AND (submitted_form_data::jsonb ->> 'mode') <> ''
    """)

    # Rows without a 'mode' key are De Novo Design runs, which always used bindcraft.
    op.execute("""
        UPDATE workflow_runs
        SET
            tool = 'bindcraft',
            workflow_id = (SELECT id FROM workflows WHERE name = 'de-novo-design')
        WHERE
            tool IS NULL
            AND (
                submitted_form_data IS NULL
                OR NOT (submitted_form_data::jsonb ? 'mode')
            )
    """)


def downgrade() -> None:
    op.drop_column('workflow_runs', 'tool')
