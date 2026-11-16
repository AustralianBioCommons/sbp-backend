"""Copy legacy repository state to Gadi without removing the rollback columns."""

from datetime import UTC, datetime
from uuid import uuid7

import sqlalchemy as sa

from alembic import op

revision = "2c5bf512f6a3"
down_revision = "230ace44f883"
branch_labels = None
depends_on = None

STATE_FIELDS = {
    "repo_staged_commit_sha": "staged_commit_sha",
    "repo_gadi_path": "location",
    "repo_staging_status": "status",
    "repo_staging_transfer_id": "transfer_id",
    "repo_staging_updated_at": "updated_at",
    "repo_staging_error_message": "error_message",
}


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    workflows = sa.Table("workflows", metadata, autoload_with=connection)
    backends = sa.Table("compute_backend", metadata, autoload_with=connection)
    repos = sa.Table("workflow_repo_status", metadata, autoload_with=connection)
    rows = (
        connection.execute(
            sa.select(workflows).where(
                sa.or_(*(workflows.c[name].is_not(None) for name in STATE_FIELDS))
            )
        )
        .mappings()
        .all()
    )
    invalid_ids = [
        str(row["id"])
        for row in rows
        if not row["repo_gadi_path"]
        or not row["repo_gadi_path"].strip()
        or row["repo_staging_status"] not in {"pending", "in_progress", "completed", "failed"}
        or (row["repo_staging_status"] == "pending" and not row["repo_staged_commit_sha"])
        or (row["repo_staging_status"] == "in_progress" and not row["repo_staging_transfer_id"])
    ]
    if invalid_ids:
        raise RuntimeError(
            "Incomplete legacy repository state; repair these workflows before migrating: "
            + ", ".join(invalid_ids)
        )
    gadi_id = connection.execute(
        sa.select(backends.c.id).where(backends.c.name == "gadi")
    ).scalar_one_or_none()
    if rows and gadi_id is None:
        raise RuntimeError("Gadi backend is required to migrate legacy repository state")

    existing = set(
        connection.scalars(
            sa.select(repos.c.workflow_id).where(repos.c.compute_backend_id == gadi_id)
        )
    )
    values = [
        {
            "id": uuid7(),
            "compute_backend_id": gadi_id,
            "workflow_id": row["id"],
            "created_at": datetime.now(UTC),
            **{target: row[source] for source, target in STATE_FIELDS.items()},
        }
        for row in rows
        if row["id"] not in existing
    ]
    if values:
        connection.execute(repos.insert(), values)


def downgrade() -> None:
    # Mirror the current Gadi state back for the old application. Keep status rows:
    # some may predate this migration or belong to newly prepared workflows.
    connection = op.get_bind()
    metadata = sa.MetaData()
    workflows = sa.Table("workflows", metadata, autoload_with=connection)
    backends = sa.Table("compute_backend", metadata, autoload_with=connection)
    repos = sa.Table("workflow_repo_status", metadata, autoload_with=connection)
    rows = connection.execute(
        sa.select(repos)
        .join(backends, repos.c.compute_backend_id == backends.c.id)
        .where(backends.c.name == "gadi")
    ).mappings()
    for row in rows:
        connection.execute(
            workflows.update()
            .where(workflows.c.id == row["workflow_id"])
            .values(**{source: row[target] for source, target in STATE_FIELDS.items()})
        )
