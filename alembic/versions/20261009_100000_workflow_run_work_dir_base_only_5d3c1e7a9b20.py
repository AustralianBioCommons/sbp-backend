"""store the base Seqera workdir on workflow_runs, without the run id suffix

Every run now records the shared work_dir (settings.seqera.work_dir), so the
column can no longer be unique. Existing rows are rewritten from
"<work_dir>/<run id>" to "<work_dir>"; downgrade appends the id back.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "5d3c1e7a9b20"
down_revision = "0a8ae645d1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_workflow_runs_work_dir", "workflow_runs", type_="unique")
    op.execute(
        """
        UPDATE workflow_runs
        SET work_dir = left(work_dir, length(work_dir) - length('/' || id::text))
        WHERE work_dir LIKE '%/' || id::text
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE workflow_runs
        SET work_dir = work_dir || '/' || id::text
        WHERE work_dir NOT LIKE '%/' || id::text
        """
    )
    op.create_unique_constraint("uq_workflow_runs_work_dir", "workflow_runs", ["work_dir"])
