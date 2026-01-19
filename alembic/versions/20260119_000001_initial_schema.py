"""Initial schema for workflows and run tracking.

Original DDL reference (from drawSQL export):
CREATE TABLE "app_users"(
    "id" UUID NOT NULL,
    "auth0_user_id" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "email" TEXT NOT NULL
);
ALTER TABLE
    "app_users" ADD PRIMARY KEY("id");
ALTER TABLE
    "app_users" ADD CONSTRAINT "app_users_auth0_user_id_unique" UNIQUE("auth0_user_id");
ALTER TABLE
    "app_users" ADD CONSTRAINT "app_users_email_unique" UNIQUE("email");
CREATE TABLE "workflows"(
    "id" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT NULL,
    "repo_url" TEXT NULL,
    "default_revision" TEXT NULL
);
ALTER TABLE
    "workflows" ADD PRIMARY KEY("id");
CREATE TABLE "workflow_runs"(
    "id" UUID NOT NULL,
    "workflow_id" UUID NULL,
    "owner_user_id" UUID NOT NULL,
    "seqera_dataset_id" TEXT NULL,
    "seqera_run_id" TEXT NOT NULL,
    "run_name" TEXT NULL,
    "work_dir" BIGINT NOT NULL
);
ALTER TABLE
    "workflow_runs" ADD PRIMARY KEY("id");
ALTER TABLE
    "workflow_runs" ADD CONSTRAINT "workflow_runs_seqera_run_id_unique" UNIQUE("seqera_run_id");
ALTER TABLE
    "workflow_runs" ADD CONSTRAINT "workflow_runs_work_dir_unique" UNIQUE("work_dir");
CREATE TABLE "s3_objects"(
    "URI" TEXT NOT NULL,
    "object_key" TEXT NOT NULL,
    "version_id" TEXT NULL,
    "size_bytes" BIGINT NULL
);
ALTER TABLE
    "s3_objects" ADD CONSTRAINT "s3_objects_uri_unique" UNIQUE("URI");
ALTER TABLE
    "s3_objects" ADD PRIMARY KEY("object_key");
CREATE TABLE "run_inputs"(
    "s3_object_id" UUID NOT NULL,
    "run_id" UUID NOT NULL
);
ALTER TABLE
    "run_inputs" ADD PRIMARY KEY("s3_object_id");
CREATE TABLE "run_outputs"(
    "s3_object_id" UUID NOT NULL,
    "run_id" UUID NOT NULL
);
ALTER TABLE
    "run_outputs" ADD PRIMARY KEY("s3_object_id");
CREATE TABLE "run_metrics"(
    "run_id" UUID NOT NULL,
    "max_score" DECIMAL(8, 2) NULL
);
ALTER TABLE
    "run_metrics" ADD PRIMARY KEY("run_id");
ALTER TABLE
    "workflow_runs" ADD CONSTRAINT "workflow_runs_id_foreign" FOREIGN KEY("id") REFERENCES "run_metrics"("run_id");
ALTER TABLE
    "run_inputs" ADD CONSTRAINT "run_inputs_run_id_foreign" FOREIGN KEY("run_id") REFERENCES "workflow_runs"("id");
ALTER TABLE
    "s3_objects" ADD CONSTRAINT "s3_objects_object_key_foreign" FOREIGN KEY("object_key") REFERENCES "run_inputs"("s3_object_id");
ALTER TABLE
    "workflow_runs" ADD CONSTRAINT "workflow_runs_owner_user_id_foreign" FOREIGN KEY("owner_user_id") REFERENCES "app_users"("id");
ALTER TABLE
    "run_outputs" ADD CONSTRAINT "run_outputs_run_id_foreign" FOREIGN KEY("run_id") REFERENCES "workflow_runs"("id");
ALTER TABLE
    "workflow_runs" ADD CONSTRAINT "workflow_runs_workflow_id_foreign" FOREIGN KEY("workflow_id") REFERENCES "workflows"("id");
ALTER TABLE
    "s3_objects" ADD CONSTRAINT "s3_objects_object_key_foreign" FOREIGN KEY("object_key") REFERENCES "run_outputs"("s3_object_id");
"""

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
        sa.Column("work_dir", sa.BigInteger(), nullable=False),
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
