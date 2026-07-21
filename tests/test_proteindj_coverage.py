"""Tests to boost coverage for proteindj executor and config modules."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, mock_open, patch

import pytest
from sqlalchemy import select

from app.db.models import QueuedJob
from app.schemas.workflows import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
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
from app.services.seqera_errors import SeqeraConfigurationError
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
        or ("outdir: s3://my-bucket/run-output-id\ninput_pdb: s3://my-bucket/inputs/test.pdb"),
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
def seqera_env(monkeypatch):
    """Set required Seqera environment variables for launch tests."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test_key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_secret")


# =============================================================================
# Tests for get_proteindj_default_params()
# =============================================================================


def test_get_proteindj_default_params_only_outdir():
    params = get_proteindj_default_params("s3://bucket/out")
    assert params == {"outdir": "s3://bucket/out"}


def test_get_proteindj_default_params_all_fields():
    params = get_proteindj_default_params(
        "s3://bucket/out",
        input_pdb="s3://bucket/in.pdb",
        hotspot_residues="A20,A21",
        num_designs=5,
        design_length="100-150",
    )
    assert params == {
        "outdir": "s3://bucket/out",
        "input_pdb": "s3://bucket/in.pdb",
        "hotspot_residues": "A20,A21",
        "num_designs": 5,
        "design_length": "100-150",
    }


def test_get_proteindj_default_params_partial_fields():
    params = get_proteindj_default_params("s3://bucket/out", num_designs=3)
    assert params == {"outdir": "s3://bucket/out", "num_designs": 3}


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


def test_get_proteindj_config_text_contains_email_and_encoded_ip():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text("/fake/proteindj.config", user_details=_USER_DETAILS)
    assert "user@ex.com" in result
    assert "MS4yLjMuNA==" in result


def test_get_proteindj_config_text_without_ip_uses_email_only():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text(
            "/fake/proteindj.config",
            user_details=_USER_DETAILS.model_copy(update={"ip_address": ""}),
        )
    assert "-A user@ex.com" in result
    assert ":" not in result.split("clusterOptions = ")[1]


def test_get_proteindj_config_text_contains_base_config():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteindj_config_text("/fake/proteindj.config", user_details=_USER_DETAILS)
    assert "base_config" in result


# =============================================================================
# Tests for _design_length()
# =============================================================================


def test_design_length_both_present():
    assert _design_length(_form_data(min_length=100, max_length=150)) == "100-150"


def test_design_length_missing_min():
    assert _design_length(_form_data(max_length=150)) is None


def test_design_length_missing_max():
    assert _design_length(_form_data(min_length=100)) is None


def test_design_length_missing_both():
    assert _design_length(_form_data()) is None


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
    assert "outdir: s3://my-bucket/run-output-id" in params_text
    assert "input_pdb: s3://my-bucket/inputs/test.pdb" in params_text
    assert "hotspot_residues: A20,A21" in params_text
    assert "num_designs: 5" in params_text
    assert "design_length: 100-150" in params_text


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
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="run-output-id",
            form_data=_form_data(),
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
        pytest.raises(SeqeraConfigurationError, match="run name"),
    ):
        await prepare_proteindj_workflow(
            form=form,
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
        pytest.raises(SeqeraConfigurationError, match="output identifier"),
    ):
        await prepare_proteindj_workflow(
            form=form,
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
        pytest.raises(SeqeraConfigurationError, match="output identifier"),
    ):
        await prepare_proteindj_workflow(
            form=form,
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/org/proteindj",
            config_path="/fake/proteindj.config",
            output_id="   ",
            form_data=_form_data(),
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
        result = await launch_proteindj_workflow(queued_job=_queued_proteindj_job())

    assert result.workflow_id == "wf_success"
    assert result.status == "submitted"
    mock_post.assert_called_once()
    posted_payload = mock_post.call_args.args[0]["launch"]
    assert "module load singularity" in posted_payload["preRunScript"]
    assert "module load nextflow" in posted_payload["preRunScript"]
    assert "export AWS_ACCESS_KEY_ID" in posted_payload["preRunScript"]


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
            queued_job=_queued_proteindj_job(prerun_script_path="/some/prerun.sh")
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
        result = await launch_proteindj_workflow(queued_job=_queued_proteindj_job(), dry_run=True)

    assert result is None
    mock_post.assert_not_called()


@pytest.mark.anyio
async def test_launch_proteindj_workflow_missing_env_var(monkeypatch, persistent_models):
    monkeypatch.delenv("SEQERA_API_URL", raising=False)
    monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)

    with pytest.raises(SeqeraConfigurationError, match="SEQERA_API_URL"):
        await launch_proteindj_workflow(queued_job=_queued_proteindj_job())
