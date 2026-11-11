import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.backends import BackendWorkflowSettings, ComputeBackend, WorkflowRepoStatus
from app.schemas.backend import (
    ComputeBackendName,
    DataTransferMode,
    RepositoryStatus,
    RepositoryTransferMode,
)
from tests.datagen import (
    BackendWorkflowSettingsFactory,
    ComputeBackendFactory,
    WorkflowFactory,
    WorkflowRepoStatusFactory,
)


def test_compute_backend_model(test_db):
    """
    Basic test of ComputeBackend construction
    """
    backend = ComputeBackend(
        name=ComputeBackendName.GADI,
        label="GADI backend",
        enabled=True,
        seqera_workspace_id="abcd1234",
        seqera_compute_env_id="gadi-backend-dev",
        env_prefix="GADI_",
        base_work_dir="/g/data/project/dev/",
        repository_transfer=RepositoryTransferMode.NATIVE,
        data_transfer=DataTransferMode.GLOBUS,
        max_workflows=10,
        service_usage_enabled=True,
    )
    test_db.add(backend)
    test_db.commit()
    test_db.refresh(backend)
    assert backend.id
    assert backend.name == ComputeBackendName.GADI.value


def test_backend_workflow_settings_model(test_db, persistent_models):
    backend = ComputeBackendFactory.build(name=ComputeBackendName.GADI.value)
    workflow = WorkflowFactory.build()
    workflow_settings = BackendWorkflowSettings(
        compute_backend=backend,
        workflow=workflow,
        enabled=True,
        config_path="configs/bindflow_gadi.conf"
    )
    test_db.add(workflow_settings)
    test_db.commit()
    test_db.refresh(workflow_settings)
    assert workflow_settings.compute_backend.name == ComputeBackendName.GADI.value
    assert workflow_settings.enabled is True


def test_backend_workflow_settings_unique(test_db, persistent_models):
    """
    Test BackendWorkflowSettings has a unique constraint on
    (compute_backend, workflow)
    """
    backend = ComputeBackendFactory.build()
    workflow = WorkflowFactory.build()
    workflow_settings = BackendWorkflowSettingsFactory.build(
        compute_backend=backend, workflow=workflow
    )
    test_db.add(workflow_settings)
    test_db.commit()
    with pytest.raises(IntegrityError):
        duplicate_settings = BackendWorkflowSettingsFactory.build(
            compute_backend=backend, workflow=workflow
        )
        test_db.add(duplicate_settings)
        test_db.flush()


def test_workflow_repo_status_model(test_db, persistent_models):
    """
    Basic test of WorkflowRepoStatus construction
    """
    backend = ComputeBackendFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    repo_status = WorkflowRepoStatus(
        compute_backend=backend,
        workflow=workflow,
        staged_commit_sha="abcd1234",
        location="/data/project/dev/my-workflow",
        status=RepositoryStatus.PENDING,
        transfer_id="efgh5678",
    )
    test_db.add(repo_status)
    test_db.commit()
    test_db.refresh(repo_status)
    assert repo_status.id
    assert repo_status.compute_backend.name == backend.name


def test_workflow_repo_status_unique(test_db, persistent_models):
    backend = ComputeBackendFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    # Existing entry
    WorkflowRepoStatusFactory.create_sync(
        compute_backend=backend, workflow=workflow
    )
    test_db.flush()
    with pytest.raises(IntegrityError):
        duplicate_status = WorkflowRepoStatusFactory.build(
            compute_backend=backend, workflow=workflow
        )
        test_db.add(duplicate_status)
        test_db.flush()
