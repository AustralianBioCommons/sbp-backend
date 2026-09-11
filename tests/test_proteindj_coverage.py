"""Tests to boost coverage for proteindj executor and config modules."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, mock_open, patch

import pytest
from sqlalchemy import select

from app.db.models import QueuedJob
from app.db.models.core import DataTransfer, RunInput, S3Object
from app.schemas.workflows.de_novo_design import ProteinDjFormData
from app.schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from app.services.proteindj_config import (
    get_proteindj_config_profiles,
    get_proteindj_config_text,
    get_proteindj_default_params,
)
from app.services.proteindj_executor import (
    _design_length,
    launch_proteindj_workflow,
    prepare_proteindj_workflow,
)
from app.services.seqera import WorkflowLaunchResult
from app.services.seqera_errors import WorkflowLaunchError
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory

_USER_DETAILS = WorkflowUserDetails(
    user_email="user@ex.com",
    ip_address="1.2.3.4",
)


def _form_data(**extra) -> WorkflowFormData:
    return WorkflowFormData(workflow="de-novo-design", tool="rfdiffusion", **extra)


def _make_launch_form(**kwargs) -> WorkflowLaunchForm:
    defaults = {
        "workflow": "de-novo-design",
        "tool": "rfdiffusion",
        "runName": "test-run",
        "paramsText": None,
    }
    defaults.update(kwargs)
    return WorkflowLaunchForm(**defaults)


def _queued_proteindj_job(
    *,
    params_text: str | None = None,
    prerun_script_path: str | None = None,
) -> QueuedJob:
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(
        name="de-novo-design",
        repo_url="https://github.com/org/proteindj",
        prerun_script_path=prerun_script_path,
    )
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)
    launch_payload = {
        "computeEnvId": "ce_456",
        "runName": "test-run",
        "pipeline": "https://github.com/org/proteindj",
        "workDir": "/work/dir",
        "workspaceId": "ws_123",
        "revision": "dev",
        "paramsText": params_text
        or ("out_dir: s3://my-bucket/run-output-id\ninput_pdb: s3://my-bucket/inputs/test.pdb"),
        "configProfiles": ["singularity"],
        "configText": "config_text",
        "resume": False,
    }
    return QueuedJobFactory.create_sync(
        workflow=workflow,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        status="pending",
    )


@contextmanager
def _mock_proteindj_db_context():
    workflow = Mock(name="workflow")
    workflow_run = Mock(name="workflow_run")
    workflow_run.workflow = workflow
    db_session = Mock(name="db_session")
    queued_job = Mock(name="queued_job")
    with patch(
        "app.services.proteindj_executor.QueuedJob", return_value=queued_job
    ) as queued_job_cls:
        yield db_session, workflow_run, workflow, queued_job_cls, queued_job


@pytest.fixture
def seqera_env(mock_settings):
    """Set required Seqera settings for launch tests."""
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "test_token"
    mock_settings.seqera.work_space = "ws_123"
    mock_settings.seqera.compute_id = "ce_456"
    mock_settings.seqera.work_dir = "/work/dir"
    mock_settings.aws.s3_bucket = "my-bucket"
    mock_settings.aws.access_key_id = "test_key"
    mock_settings.aws.secret_access_key = "test_secret"
    return mock_settings


def _proteindj_form_kwargs(**overrides) -> dict:
    defaults = {
        "workflow": "de-novo-design",
        "tool": "rfdiffusion",
        "starting_pdb": "s3://bucket/in.pdb",
        "target_hotspot_residues": "A20,A21",
        "number_of_final_designs": 5,
        "min_length": 100,
        "max_length": 150,
    }
    defaults.update(overrides)
    return defaults


# =============================================================================
# Tests for ProteinDjFormData validation
# =============================================================================


def test_proteindj_form_data_accepts_valid_fields():
    fields = ProteinDjFormData(**_proteindj_form_kwargs())
    assert fields.min_length == 100
    assert fields.max_length == 150


def test_proteindj_form_data_rejects_too_many_hotspot_residues():
    with pytest.raises(ValueError, match="Too many hotspot residues"):
        ProteinDjFormData(
            **_proteindj_form_kwargs(target_hotspot_residues="A1,A2,A3,A4,A5,A6,A7,A8,A9")
        )


def test_proteindj_form_data_allows_exactly_max_hotspot_residues():
    fields = ProteinDjFormData(
        **_proteindj_form_kwargs(target_hotspot_residues="A1,A2,A3,A4,A5,A6,A7,A8")
    )
    assert fields.target_hotspot_residues == "A1,A2,A3,A4,A5,A6,A7,A8"


def test_proteindj_form_data_rejects_min_length_below_floor():
    with pytest.raises(ValueError, match="min_length"):
        ProteinDjFormData(**_proteindj_form_kwargs(min_length=64, max_length=150))


def test_proteindj_form_data_rejects_max_length_above_ceiling():
    with pytest.raises(ValueError, match="max_length"):
        ProteinDjFormData(**_proteindj_form_kwargs(min_length=65, max_length=151))


def test_proteindj_form_data_rejects_min_length_above_max_length():
    with pytest.raises(ValueError, match="min_length must not exceed max_length"):
        ProteinDjFormData(**_proteindj_form_kwargs(min_length=150, max_length=100))


def test_proteindj_form_data_allows_boundary_lengths():
    fields = ProteinDjFormData(**_proteindj_form_kwargs(min_length=65, max_length=150))
    assert fields.min_length == 65
    assert fields.max_length == 150


# =============================================================================
# Tests for get_proteindj_default_params()
# =============================================================================


def test_get_proteindj_default_params_all_fields():
    params = get_proteindj_default_params(
        "s3://bucket/out",
        input_pdb="s3://bucket/in.pdb",
        hotspot_residues="A20,A21",
        num_designs=5,
        design_length="100-150",
    )
    assert params == {
        "out_dir": "s3://bucket/out",
        "input_pdb": "s3://bucket/in.pdb",
        "hotspot_residues": "A20,A21",
        "num_designs": 5,
        "design_length": "100-150",
    }


def test_get_proteindj_default_params_missing_required_field_raises():
    with pytest.raises(TypeError):
        get_proteindj_default_params("s3://bucket/out", num_designs=3)  # type: ignore[call-arg]


# =============================================================================
# Tests for get_proteindj_config_profiles()
# =============================================================================


def test_get_proteindj_config_profiles_returns_list():
    profiles = get_proteindj_config_profiles()
    assert isinstance(profiles, list)


def test_get_proteindj_config_profiles_contains_singularity():
    assert "singularity" in get_proteindj_config_profiles()


# =============================================================================
# Tests for get_proteindj_config_text()
# =============================================================================


def test_get_proteindj_config_text_appends_process_block():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text("/fake/proteindj.config", user_details=_USER_DETAILS)
    assert "process {" in result
    assert "clusterOptions" in result


def test_get_proteindj_config_text_contains_encoded_email_and_encoded_ip():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text("/fake/proteindj.config", user_details=_USER_DETAILS)
    assert "dXNlckBleC5jb20=" in result
    assert "MS4yLjMuNA==" in result


def test_get_proteindj_config_text_without_ip_uses_encoded_email_only():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text(
            "/fake/proteindj.config",
            user_details=_USER_DETAILS.model_copy(update={"ip_address": ""}),
        )
    assert "-A dXNlckBleC5jb20=" in result
    assert ":" not in result.split("clusterOptions = ")[1]


def test_get_proteindj_config_text_contains_base_config():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text("/fake/proteindj.config", user_details=_USER_DETAILS)
    assert "base_config" in result


# =============================================================================
# Tests for _design_length()
# =============================================================================


def test_design_length_formats_min_max_range():
    fields = ProteinDjFormData(
        workflow="de-novo-design",
        tool="rfdiffusion",
        starting_pdb="s3://bucket/in.pdb",
        target_hotspot_residues="A20",
        number_of_final_designs=5,
        min_length=100,
        max_length=150,
    )
    assert _design_length(fields) == "100-150"


# =============================================================================
# Tests for prepare_proteindj_workflow()
# =============================================================================


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_writes_expected_queued_job(
    test_db, persistent_models, seqera_env
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="de-novo-design")
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)

    form = _make_launch_form(runName="queued-proteindj-run")
    form_data = _form_data(
        starting_pdb="s3://my-bucket/inputs/test.pdb",
        target_hotspot_residues="A20,A21",
        number_of_final_designs=5,
        min_length=100,
        max_length=150,
    )

    with (
        patch(
            "app.services.proteindj_executor.get_proteindj_config_text",
            return_value="config_text",
        ),
        patch(
            "app.services.proteindj_executor.get_proteindj_config_profiles",
            return_value=["singularity"],
        ),
    ):
        prepared_job = await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            revision="main",
            output_id="run-output-id",
            form_data=form_data,
            user_details=_USER_DETAILS.model_copy(
                update={"user_email": "test@example.com", "ip_address": "127.0.0.1"}
            ),
        )

    queued_job = test_db.scalar(
        select(QueuedJob).where(QueuedJob.workflow_run_id == workflow_run.id)
    )
    assert queued_job is not None
    assert queued_job.workflow_id == workflow.id
    assert queued_job.workflow_run_id == workflow_run.id
    assert queued_job.status == "pending"
    assert queued_job.next_attempt_at is not None
    assert queued_job.id == prepared_job.id
    assert queued_job.launch_payload["computeEnvId"] == "ce_456"
    assert queued_job.launch_payload["runName"] == "queued-proteindj-run"
    assert queued_job.launch_payload["pipeline"] == "https://github.com/org/proteindj"
    assert queued_job.launch_payload["workDir"] == "/work/dir"
    assert queued_job.launch_payload["workspaceId"] == "ws_123"
    assert queued_job.launch_payload["revision"] == "main"
    assert queued_job.launch_payload["configProfiles"] == ["singularity"]
    assert queued_job.launch_payload["configText"] == "config_text"
    assert "preRunScript" not in queued_job.launch_payload
    assert queued_job.launch_payload["resume"] is False
    params_text = queued_job.launch_payload["paramsText"]
    staged_pdb_location = f"/test/input/de-novo-design/{workflow_run.id}/test.pdb"
    assert "out_dir: /test/output/de-novo-design/run-output-id" in params_text
    assert f"input_pdb: {staged_pdb_location}" in params_text
    assert "hotspot_residues: A20,A21" in params_text
    assert "num_designs: 5" in params_text
    assert "design_length: 100-150" in params_text

    # The uploaded starting-pdb file gets its own Globus staging record, separate
    # from the main samplesheet input handled in the workflows route.
    pdb_transfer = test_db.scalar(
        select(DataTransfer).where(
            DataTransfer.workflow_run_id == workflow_run.id,
            DataTransfer.source_location == "s3://my-bucket/inputs/test.pdb",
        )
    )
    assert pdb_transfer is not None
    assert pdb_transfer.direction == "input"
    assert pdb_transfer.provider == "globus"
    assert pdb_transfer.destination_location == staged_pdb_location
    assert pdb_transfer.status == "pending"

    run_input = test_db.scalar(select(RunInput).where(RunInput.data_transfer_id == pdb_transfer.id))
    assert run_input is not None
    assert run_input.run_id == workflow_run.id
    assert run_input.s3_object_id == "inputs/test.pdb"

    s3_object = test_db.get(S3Object, "inputs/test.pdb")
    assert s3_object is not None
    assert s3_object.uri == "s3://my-bucket/inputs/test.pdb"


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_appends_custom_params_text(
    test_db, persistent_models, seqera_env
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="de-novo-design")
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)

    form = _make_launch_form(paramsText="extra_param: value")

    with (
        patch("app.services.proteindj_executor.get_proteindj_config_text", return_value=""),
        patch(
            "app.services.proteindj_executor.get_proteindj_config_profiles",
            return_value=["singularity"],
        ),
    ):
        prepared_job = await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(
                starting_pdb="s3://my-bucket/inputs/test.pdb",
                target_hotspot_residues="A20,A21",
                number_of_final_designs=5,
                min_length=100,
                max_length=150,
            ),
            user_details=_USER_DETAILS,
        )

    queued_job = test_db.scalar(
        select(QueuedJob).where(QueuedJob.workflow_run_id == workflow_run.id)
    )
    assert "extra_param: value" in queued_job.launch_payload["paramsText"]
    assert prepared_job.id == queued_job.id


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_run_name(seqera_env):
    form = _make_launch_form(runName="  ")
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="run name"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_output_id(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="output identifier"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id=None,
            form_data=_form_data(),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_empty_output_id(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="output identifier"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="   ",
            form_data=_form_data(),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_starting_pdb(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="starting_pdb"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(
                target_hotspot_residues="A20,A21",
                number_of_final_designs=5,
                min_length=100,
                max_length=150,
            ),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_hotspot_residues(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="target_hotspot_residues"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(
                starting_pdb="s3://my-bucket/inputs/test.pdb",
                number_of_final_designs=5,
                min_length=100,
                max_length=150,
            ),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_num_designs(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="number_of_final_designs"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(
                starting_pdb="s3://my-bucket/inputs/test.pdb",
                target_hotspot_residues="A20,A21",
                min_length=100,
                max_length=150,
            ),
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_prepare_proteindj_workflow_missing_design_length(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteindj_db_context() as (db_session, workflow_run, *_),
        pytest.raises(WorkflowLaunchError, match="min_length"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            settings=seqera_env,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(
                starting_pdb="s3://my-bucket/inputs/test.pdb",
                target_hotspot_residues="A20,A21",
                number_of_final_designs=5,
            ),
            user_details=_USER_DETAILS,
        )


# =============================================================================
# Tests for launch_proteindj_workflow()
# =============================================================================


@pytest.mark.anyio
async def test_launch_proteindj_workflow_success(seqera_env, persistent_models):
    expected_result = WorkflowLaunchResult(
        workflow_id="wf_success", status="submitted", message=None
    )

    with (
        patch(
            "app.services.proteindj_executor.post_seqera_launch",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_post,
    ):
        result = await launch_proteindj_workflow(
            queued_job=_queued_proteindj_job(), settings=seqera_env
        )

    assert result.workflow_id == "wf_success"
    assert result.status == "submitted"
    mock_post.assert_called_once()
    posted_payload = mock_post.call_args.args[0]["launch"]
    assert "module load singularity" in posted_payload["preRunScript"]
    assert "module load nextflow" in posted_payload["preRunScript"]


@pytest.mark.anyio
async def test_launch_proteindj_workflow_with_prerun_script_path(seqera_env, persistent_models):
    expected_result = WorkflowLaunchResult(workflow_id="wf_prerun", status="submitted")

    with (
        patch(
            "app.services.proteindj_executor.post_seqera_launch",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_post,
        patch(
            "app.services.proteindj_executor.get_executor_script",
            return_value="prerun_body",
        ) as mock_script,
    ):
        result = await launch_proteindj_workflow(
            queued_job=_queued_proteindj_job(prerun_script_path="/some/prerun.sh"),
            settings=seqera_env,
        )

    assert result.workflow_id == "wf_prerun"
    posted_payload = mock_post.call_args.args[0]["launch"]
    assert posted_payload["preRunScript"] == "prerun_body"
    assert mock_script.call_args.kwargs["prerun_script_path"] == "/some/prerun.sh"


@pytest.mark.anyio
async def test_launch_proteindj_workflow_dry_run(seqera_env, persistent_models):
    with patch(
        "app.services.proteindj_executor.post_seqera_launch",
        new_callable=AsyncMock,
    ) as mock_post:
        result = await launch_proteindj_workflow(
            queued_job=_queued_proteindj_job(), settings=seqera_env, dry_run=True
        )

    assert result is None
    mock_post.assert_not_called()
