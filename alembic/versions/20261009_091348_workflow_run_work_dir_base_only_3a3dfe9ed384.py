"""workflow run work_dir base only"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3a3dfe9ed384'
down_revision = '0a8ae645d1b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every run now records the shared Seqera work_dir, so it can't be unique.
    # (Autogenerate also proposed dropping the workflows config_path check
    # constraints - false positives, they're raw-SQL constraints not on the model.)
    op.drop_constraint(op.f('uq_workflow_runs_work_dir'), 'workflow_runs', type_='unique')
    # Rewrite existing "<work_dir>/<run id>" rows to "<work_dir>".
    op.execute(
        "UPDATE workflow_runs "
        "SET work_dir = left(work_dir, length(work_dir) - length('/' || id::text)) "
        "WHERE work_dir LIKE '%/' || id::text"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workflow_runs SET work_dir = work_dir || '/' || id::text "
        "WHERE work_dir NOT LIKE '%/' || id::text"
    )
    op.create_unique_constraint(op.f('uq_workflow_runs_work_dir'), 'workflow_runs', ['work_dir'], postgresql_nulls_not_distinct=False)
