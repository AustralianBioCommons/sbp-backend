"""Initial schema for workflows and run tracking."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260119_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("auth0_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth0_user_id", name="app_users_auth0_user_id_unique"),
        sa.UniqueConstraint("email", name="app_users_email_unique"),
    )

    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("repo_url", sa.Text(), nullable=True),
        sa.Column("default_revision", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seqera_dataset_id", sa.Text(), nullable=True),
        sa.Column("seqera_run_id", sa.Text(), nullable=False),
        sa.Column("run_name", sa.Text(), nullable=True),
        sa.Column("work_dir", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_users.id"], name="workflow_runs_owner_user_id_foreign"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], name="workflow_runs_workflow_id_foreign"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seqera_run_id", name="workflow_runs_seqera_run_id_unique"),
        sa.UniqueConstraint("work_dir", name="workflow_runs_work_dir_unique"),
    )

    op.create_table(
        "s3_objects",
        sa.Column("URI", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("object_key"),
        sa.UniqueConstraint("URI", name="s3_objects_uri_unique"),
    )

    op.create_table(
        "run_metrics",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("max_score", sa.Numeric(8, 2), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], name="run_metrics_run_id_foreign"),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "run_inputs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("s3_object_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], name="run_inputs_run_id_foreign"),
        sa.ForeignKeyConstraint(["s3_object_id"], ["s3_objects.object_key"], name="run_inputs_s3_object_id_foreign"),
        sa.PrimaryKeyConstraint("run_id", "s3_object_id", name="run_inputs_pkey"),
    )

    op.create_table(
        "run_outputs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("s3_object_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], name="run_outputs_run_id_foreign"),
        sa.ForeignKeyConstraint(["s3_object_id"], ["s3_objects.object_key"], name="run_outputs_s3_object_id_foreign"),
        sa.PrimaryKeyConstraint("run_id", "s3_object_id", name="run_outputs_pkey"),
    )


def downgrade() -> None:
    op.drop_table("run_outputs")
    op.drop_table("run_inputs")
    op.drop_table("run_metrics")
    op.drop_table("s3_objects")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
    op.drop_table("app_users")
