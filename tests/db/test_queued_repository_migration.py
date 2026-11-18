"""Schema-only repository snapshots leave existing queue payloads unchanged."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import load_python_file

from app.db.models import QueuedJob
from app.schemas.backend import ComputeBackendName
from tests.datagen import (
    AppUserFactory,
    ComputeBackendFactory,
    QueuedJobFactory,
    WorkflowFactory,
    WorkflowRepoStatusFactory,
    WorkflowRunFactory,
)


@pytest.fixture
def legacy_queue(test_db, persistent_models, monkeypatch):
    connection = test_db.connection()
    monkeypatch.setitem(connection.dialect.ischema_names, "UUID", sa.UUID)
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("queued_jobs") as batch:
        batch.drop_column("repository_ready")
        batch.drop_column("repository_location")
        batch.drop_column("repository_transfer")
    test_db.commit()
    migration = load_python_file(
        str(Path(__file__).resolve().parents[2] / "alembic" / "versions"),
        "20261117_101451_snapshot_repository_preparation_on__abe9d620e419.py",
    )
    backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    workflow = WorkflowFactory.create_sync()
    owner = AppUserFactory.create_sync()
    jobs = sa.Table("queued_jobs", sa.MetaData(), autoload_with=test_db.connection())

    def create_job(script, status="staging"):
        run = WorkflowRunFactory.create_sync(workflow=workflow, backend=backend, owner=owner)
        job = QueuedJobFactory.build(workflow_id=workflow.id, workflow_run_id=run.id, status=status)
        test_db.execute(
            jobs.insert().values(
                id=job.id,
                workflow_id=workflow.id,
                workflow_run_id=run.id,
                launch_payload={
                    "pipeline": workflow.repo_url,
                    "revision": "dev",
                    "preRunScript": script,
                },
                status=status,
                attempts=0,
                queued_at=datetime.now(UTC),
            )
        )
        test_db.commit()
        return job.id

    return migration, backend, workflow, create_job


@pytest.mark.parametrize("status", ["staging", "pending", "launching"])
@pytest.mark.parametrize(
    "script",
    [
        "module load nextflow\n",
        "export NXF_OFFLINE=true\nexport NXF_ASSETS=/old/checkout/\n",
    ],
)
def test_existing_jobs_receive_defaults_without_backfill(test_db, legacy_queue, status, script):
    migration, backend, workflow, create_job = legacy_queue
    WorkflowRepoStatusFactory.create_sync(
        workflow=workflow,
        compute_backend=backend,
        location="/different/checkout/owner/repo",
        status="completed",
    )
    job_id = create_job(script, status=status)
    with Operations.context(MigrationContext.configure(test_db.connection())):
        migration.upgrade()
    job = test_db.get(QueuedJob, job_id)
    assert job.repository_transfer == "native"
    assert job.repository_location is None
    assert job.repository_ready is False
    assert job.status == status
    assert job.launch_payload == {
        "pipeline": workflow.repo_url,
        "revision": "dev",
        "preRunScript": script,
    }


def test_downgrade_retains_launch_payload(test_db, legacy_queue):
    migration, backend, workflow, create_job = legacy_queue
    job_id = create_job("module load nextflow\n")
    with Operations.context(MigrationContext.configure(test_db.connection())):
        migration.upgrade()
        migration.downgrade()
    jobs = sa.Table("queued_jobs", sa.MetaData(), autoload_with=test_db.connection())
    payload = test_db.scalar(sa.select(jobs.c.launch_payload).where(jobs.c.id == job_id))
    assert payload["preRunScript"] == "module load nextflow\n"
