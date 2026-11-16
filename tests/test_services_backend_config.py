"""Backend configuration precedence and preparation integration."""

import shlex
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.backend import ComputeBackendName, DataTransferMode, RepositoryTransferMode
from app.schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from app.services.compute_backends import (
    resolve_backend_launch_config,
    validate_backend_launch_config,
)
from app.services.proteindj_executor import prepare_proteindj_workflow
from app.services.proteinfold_executor import prepare_proteinfold_workflow
from app.services.seqera_errors import WorkflowLaunchError
from app.services.wisps_executor import prepare_wisps_workflow
from tests.datagen import (
    AppUserFactory,
    BackendWorkflowSettingsFactory,
    ComputeBackendFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


@pytest.fixture
def backend_context(test_db, persistent_models, mock_settings, monkeypatch):
    for field, value in mock_settings.globus.model_dump().items():
        monkeypatch.setenv(f"TEST_GLOBUS_{field.upper()}", str(value))
    backend = ComputeBackendFactory.create_sync(
        name=ComputeBackendName.GADI,
        enabled=True,
        env_prefix="TEST_",
        seqera_workspace_id="backend-workspace",
        seqera_compute_env_id="backend-env",
        base_work_dir="/backend/work",
        project_id="backend-project",
    )
    workflow = WorkflowFactory.create_sync(config_path="/global.config", ref_database="/global/db")
    return backend, workflow


@pytest.mark.parametrize(
    "values, expected",
    [
        (None, ("/global.config", "/global/db")),
        ({"config_path": None, "ref_database_dir": None}, ("/global.config", "/global/db")),
        (
            {"config_path": "/custom.config", "ref_database_dir": None},
            ("/custom.config", "/global/db"),
        ),
        ({"config_path": None, "ref_database_dir": "/custom/db"}, ("/global.config", "/custom/db")),
        (
            {"config_path": "/custom.config", "ref_database_dir": "/custom/db"},
            ("/custom.config", "/custom/db"),
        ),
    ],
)
def test_resolves_overrides_per_field(test_db, backend_context, values, expected):
    backend, workflow = backend_context
    if values is not None:
        BackendWorkflowSettingsFactory.create_sync(
            compute_backend=backend, workflow=workflow, enabled=True, **values
        )
    config = resolve_backend_launch_config(test_db, backend, workflow)
    assert (config.config_path, config.ref_database_dir) == expected
    assert config.workspace_id == "backend-workspace"
    assert config.compute_env_id == "backend-env"
    assert config.base_work_dir == "/backend/work"
    assert config.project_id == "backend-project"
    assert workflow.config_path == "/global.config"
    assert workflow.ref_database == "/global/db"


def test_other_workflow_override_is_ignored(test_db, backend_context):
    backend, workflow = backend_context
    other = WorkflowFactory.create_sync()
    BackendWorkflowSettingsFactory.create_sync(
        compute_backend=backend,
        workflow=other,
        enabled=False,
    )
    assert resolve_backend_launch_config(test_db, backend, workflow).config_path == "/global.config"


@pytest.mark.parametrize("field", ["seqera_workspace_id", "seqera_compute_env_id", "base_work_dir"])
def test_empty_backend_config_rejected(test_db, backend_context, field):
    backend, workflow = backend_context
    setattr(backend, field, "   ")
    with pytest.raises(ValidationError):
        resolve_backend_launch_config(test_db, backend, workflow)


@pytest.mark.parametrize("field", ["config_path", "ref_database_dir"])
def test_empty_override_does_not_fall_back(test_db, backend_context, field):
    backend, workflow = backend_context
    BackendWorkflowSettingsFactory.create_sync(
        compute_backend=backend,
        workflow=workflow,
        enabled=True,
        **({"config_path": None, "ref_database_dir": None} | {field: ""}),
    )
    with pytest.raises(ValidationError):
        resolve_backend_launch_config(test_db, backend, workflow)


@pytest.mark.parametrize("disabled", ["backend", "workflow"])
def test_disabled_config_rejected(test_db, backend_context, disabled):
    backend, workflow = backend_context
    if disabled == "backend":
        backend.enabled = False
    else:
        BackendWorkflowSettingsFactory.create_sync(
            compute_backend=backend,
            workflow=workflow,
            enabled=False,
        )
    with pytest.raises(ValueError, match="disabled"):
        resolve_backend_launch_config(test_db, backend, workflow)


def test_run_backend_mismatch_rejected(test_db, backend_context):
    backend, workflow = backend_context
    config = resolve_backend_launch_config(test_db, backend, workflow)
    run = WorkflowRunFactory.build(backend_id=uuid4(), workflow_id=workflow.id)
    with pytest.raises(WorkflowLaunchError, match="does not match"):
        validate_backend_launch_config(run, backend, config)


@pytest.mark.parametrize("name", list(ComputeBackendName))
@pytest.mark.parametrize("repo_mode", list(RepositoryTransferMode))
@pytest.mark.parametrize(
    "workflow_name, prepare",
    [
        ("single-prediction", prepare_proteinfold_workflow),
        ("de-novo-design", prepare_proteindj_workflow),
        ("interaction-screening", prepare_wisps_workflow),
    ],
)
async def test_preparation_uses_backend_config(
    test_db,
    backend_context,
    mock_settings,
    tmp_path,
    monkeypatch,
    name,
    repo_mode,
    workflow_name,
    prepare,
):
    backend, workflow = backend_context
    backend.name = name
    backend.repository_transfer = repo_mode
    workflow.name = workflow_name
    workflow.prerun_script_path = None
    workflow.repo_gadi_path = "/checkout/owner/repo"
    custom_config = tmp_path / "backend.config"
    custom_config.write_text("// backend-specific configuration\n")
    BackendWorkflowSettingsFactory.create_sync(
        compute_backend=backend,
        workflow=workflow,
        enabled=True,
        config_path=str(custom_config),
        ref_database_dir="/backend/db with spaces",
    )
    monkeypatch.setenv("TEST_GLOBUS_OUTPUT_DIR", "/backend/output")
    monkeypatch.setenv("TEST_GLOBUS_INPUT_DIR", "/backend/input")
    config = resolve_backend_launch_config(test_db, backend, workflow)
    run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        backend=backend,
        owner=AppUserFactory.create_sync(),
        work_dir=f"{config.base_work_dir}/run-id",
    )
    tool = "rfdiffusion" if workflow_name == "de-novo-design" else "boltz"
    form = WorkflowLaunchForm(workflow=workflow_name, tool=tool, runName="backend-test")
    form_data = WorkflowFormData(
        workflow=workflow_name,
        tool=tool,
        starting_pdb="s3://bucket/input.pdb",
        target_hotspot_residues="A20",
        number_of_final_designs=1,
        min_length=100,
        max_length=150,
    )
    kwargs = (
        {}
        if workflow_name == "de-novo-design"
        else {"staged_input_location": "/backend/input/sheet.csv"}
    )
    job = await prepare(
        form,
        settings=mock_settings,
        db_session=test_db,
        workflow_run=run,
        pipeline=workflow.repo_url,
        backend=backend,
        launch_config=config,
        output_id=str(run.id),
        form_data=form_data,
        user_details=WorkflowUserDetails(user_email="test@example.com", ip_address="1.2.3.4"),
        **kwargs,
    )
    payload = job.launch_payload
    assert payload["workspaceId"] == "backend-workspace"
    assert payload["computeEnvId"] == "backend-env"
    assert payload["workDir"] == "/backend/work/run-id"
    assert "/backend/output/" in payload["paramsText"]
    assert "// backend-specific configuration" in payload["configText"]
    assert ("clusterOptions" in payload["configText"]) == (name == ComputeBackendName.GADI)
    assert ("module load" in payload["preRunScript"]) == (name == ComputeBackendName.GADI)
    assert ("NXF_OFFLINE" in payload["preRunScript"]) == (
        repo_mode == RepositoryTransferMode.GLOBUS
    )
    assert ("NXF_ASSETS" in payload["preRunScript"]) == (repo_mode == RepositoryTransferMode.GLOBUS)
    if workflow_name != "de-novo-design":
        assert shlex.quote("/backend/db with spaces") in payload["preRunScript"]
        if name == ComputeBackendName.GADI:
            assert "-P backend-project" in payload["configText"]
    else:
        assert "/backend/input/" in payload["paramsText"]


@pytest.mark.parametrize("repo_mode", list(RepositoryTransferMode))
def test_native_data_preparation_is_explicitly_unsupported(test_db, backend_context, repo_mode):
    backend, workflow = backend_context
    backend.data_transfer = DataTransferMode.NATIVE
    backend.repository_transfer = repo_mode
    config = resolve_backend_launch_config(test_db, backend, workflow)
    with pytest.raises(ValueError, match="Native input/output preparation"):
        config.require_globus()


def test_new_unflushed_run_uses_relationship_ids(test_db, backend_context):
    backend, workflow = backend_context
    config = resolve_backend_launch_config(test_db, backend, workflow)
    run = WorkflowRunFactory.build(
        backend=backend,
        workflow=workflow,
        backend_id=None,
        workflow_id=None,
        work_dir="/backend/work/new",
    )
    assert run.backend_id is None
    assert run.workflow_id is None
    validate_backend_launch_config(run, backend, config)
