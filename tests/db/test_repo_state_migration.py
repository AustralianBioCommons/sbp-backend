"""Run the generated migration against populated legacy database state."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import load_python_file

from app.db.models import Workflow, WorkflowRepoStatus
from app.schemas.backend import ComputeBackendName
from tests.datagen import ComputeBackendFactory, WorkflowFactory, WorkflowRepoStatusFactory


@pytest.fixture
def repo_migration(test_db, persistent_models, monkeypatch):
    connection = test_db.connection()
    monkeypatch.setitem(connection.dialect.ischema_names, "UUID", sa.UUID)
    return load_python_file(
        str(Path(__file__).resolve().parents[2] / "alembic" / "versions"),
        "20261116_161424_migrate_workflow_repository_state_to__2c5bf512f6a3.py",
    )


def run_upgrade(test_db, migration):
    with Operations.context(MigrationContext.configure(test_db.connection())):
        migration.upgrade()
    test_db.expire_all()


def test_migrates_all_legacy_repository_fields(test_db, repo_migration):
    gadi = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    workflows = []
    timestamp = datetime(2026, 10, 12, 3, 4, 5, 123456, tzinfo=UTC)
    for status in ("pending", "in_progress", "completed", "failed"):
        workflows.append(
            WorkflowFactory.create_sync(
                repo_staged_commit_sha=f"commit-{status}",
                repo_gadi_path=f"/existing/checkouts/{status}/owner/repo",
                repo_staging_status=status,
                repo_staging_transfer_id=f"persisted-{status}-".ljust(48, "x"),
                repo_staging_updated_at=timestamp,
                repo_staging_error_message=f"diagnostic-{status}",
            )
        )
    untouched = WorkflowFactory.create_sync()
    run_upgrade(test_db, repo_migration)

    repos = test_db.scalars(sa.select(WorkflowRepoStatus)).all()
    assert len(repos) == 4
    for workflow in workflows:
        repo = WorkflowRepoStatus.get_for_workflow(test_db, gadi.id, workflow.id)
        assert repo is not None
        assert repo.staged_commit_sha == workflow.repo_staged_commit_sha
        assert repo.location == workflow.repo_gadi_path
        assert repo.status == workflow.repo_staging_status
        assert repo.transfer_id == workflow.repo_staging_transfer_id
        assert repo.updated_at == workflow.repo_staging_updated_at
        assert repo.error_message == workflow.repo_staging_error_message
    assert WorkflowRepoStatus.get_for_workflow(test_db, gadi.id, untouched.id) is None
    columns = {
        c["name"]: c for c in sa.inspect(test_db.connection()).get_columns("workflow_repo_status")
    }
    assert isinstance(columns["transfer_id"]["type"], sa.String)
    assert columns["transfer_id"]["type"].length == 48
    # Legacy columns remain intact for the later cleanup migration.
    assert "repo_gadi_path" in {
        c["name"] for c in sa.inspect(test_db.connection()).get_columns("workflows")
    }


@pytest.mark.parametrize(
    "legacy",
    [
        {"repo_staging_status": "completed"},
        {"repo_gadi_path": "/checkout"},
        {"repo_gadi_path": "/checkout", "repo_staging_status": "pending"},
        {"repo_gadi_path": "/checkout", "repo_staging_status": "in_progress"},
        {"repo_staging_error_message": "failed before a path was known"},
    ],
)
def test_incomplete_legacy_state_stops_before_copying(test_db, repo_migration, legacy):
    ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    WorkflowFactory.create_sync(repo_gadi_path="/valid", repo_staging_status="completed")
    malformed = WorkflowFactory.create_sync(**legacy)
    with pytest.raises(RuntimeError, match=str(malformed.id)):
        run_upgrade(test_db, repo_migration)
    assert test_db.scalar(sa.select(sa.func.count()).select_from(WorkflowRepoStatus)) == 0


def test_existing_backend_state_is_not_overwritten(test_db, repo_migration):
    gadi = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    workflow = WorkflowFactory.create_sync(
        repo_gadi_path="/legacy", repo_staging_status="completed"
    )
    current = WorkflowRepoStatusFactory.create_sync(
        workflow=workflow,
        compute_backend=gadi,
        status="in_progress",
        location="/current",
        transfer_id="active-current-task",
        staged_commit_sha="current-sha",
    )
    run_upgrade(test_db, repo_migration)
    assert current.location == "/current"
    assert current.transfer_id == "active-current-task"
    assert current.status == "in_progress"
    assert test_db.scalar(sa.select(sa.func.count()).select_from(WorkflowRepoStatus)) == 1


def test_missing_gadi_stops_migration(test_db, repo_migration):
    WorkflowFactory.create_sync(repo_gadi_path="/legacy", repo_staging_status="completed")
    with pytest.raises(RuntimeError, match="Gadi backend is required"):
        run_upgrade(test_db, repo_migration)


def test_downgrade_restores_latest_state_for_legacy_readers(test_db, repo_migration):
    gadi = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    workflow = WorkflowFactory.create_sync(
        repo_gadi_path="/legacy", repo_staging_status="completed"
    )
    run_upgrade(test_db, repo_migration)
    repo = WorkflowRepoStatus.get_for_workflow(test_db, gadi.id, workflow.id)
    repo.mark_failed("new diagnostic")
    test_db.commit()
    with Operations.context(MigrationContext.configure(test_db.connection())):
        repo_migration.downgrade()
    test_db.expire_all()
    assert test_db.get(Workflow, workflow.id).repo_staging_status == "failed"
    assert workflow.repo_staging_error_message == "new diagnostic"
