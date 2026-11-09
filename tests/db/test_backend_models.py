from app.db.models.backends import BackendWorkflowSettings, ComputeBackend
from app.schemas.backend import ComputeBackendName, DataTransferMode, RepositoryTransferMode
from tests.datagen import ComputeBackendFactory, WorkflowFactory


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
