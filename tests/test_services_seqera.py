"""Tests for Seqera service."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import QueuedJob
from app.schemas.workflows import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from app.services.bindflow_executor import (
    _get_required_env,
    launch_bindflow_workflow,
    prepare_bindflow_workflow,
)
from app.services.seqera import WorkflowExecutorError, WorkflowLaunchResult
from app.services.seqera_errors import SeqeraConfigurationError
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory

_CONFIG_PATH = "/some/bindflow.config"
_USER_DETAILS = WorkflowUserDetails(
    user_email="test@example.com",
    full_name="Test_User",
    institute="example.com",
    ip_address="127.0.0.1",
)


def _empty_form_data() -> WorkflowFormData:
    return WorkflowFormData(workflow="de-novo-design", tool="bindcraft")


def _queued_bindflow_job(
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
        "computeEnvId": "test_compute_env_id",
        "runName": "seqera-test-run",
        "pipeline": "https://github.com/test/repo",
        "workDir": "/test/work/dir",
        "workspaceId": "test_workspace_id",
        "revision": "dev",
        "paramsText": params_text
        or (
            "project: yz52\n"
            "outdir: s3://test-s3-bucket/run-out\n"
            "input: s3://test-s3-bucket/inputs/samplesheets/test.csv\n"
            "mode: bindcraft"
        ),
        "configProfiles": ["gadi"],
        "configText": "",
        "resume": False,
    }
    return QueuedJobFactory.create_sync(
        workflow=workflow,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        status="pending",
    )


@contextmanager
def _mock_bindflow_db_context():
    workflow = Mock(name="workflow")
    workflow_run = Mock(name="workflow_run")
    workflow_run.workflow = workflow
    db_session = Mock(name="db_session")
    queued_job = Mock(name="queued_job")
    with patch(
        "app.services.bindflow_executor.QueuedJob", return_value=queued_job
    ) as queued_job_cls:
        yield db_session, workflow_run, workflow, queued_job_cls, queued_job


@pytest.fixture(autouse=True)
def mock_bindflow_config_text():
    """Prevent get_bindflow_config_text from trying to open a real file."""
    with patch("app.services.bindflow_executor.get_bindflow_config_text", return_value=""):
        yield


def test_get_existing_env_variable():
    """Test getting an existing environment variable."""
    result = _get_required_env("SEQERA_API_URL")
    assert result == "https://api.seqera.test"


def test_get_missing_env_variable():
    """Test that missing env variable raises error."""
    with pytest.raises(SeqeraConfigurationError, match="MISSING_VAR"):
        _get_required_env("MISSING_VAR")


@pytest.mark.asyncio
@respx.mock
async def test_launch_success_minimal(persistent_models):
    """Test successful workflow launch with minimal parameters."""
    route = respx.post(url__regex=r"https://api\.seqera\.test/workflow/launch.*").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_test_123"},
        )
    )

    result = await launch_bindflow_workflow(queued_job=_queued_bindflow_job())

    assert isinstance(result, WorkflowLaunchResult)
    assert result.workflow_id == "wf_test_123"
    assert result.status == "submitted"
    assert route.called
    assert route.call_count == 1
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert "module load singularity" in payload["launch"]["preRunScript"]
    assert "module load nextflow" in payload["launch"]["preRunScript"]
    assert "export AWS_ACCESS_KEY_ID" in payload["launch"]["preRunScript"]


@pytest.mark.asyncio
async def test_prepare_bindflow_workflow_writes_expected_queued_job(
    test_db, persistent_models, monkeypatch
):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test_key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)

    form = WorkflowLaunchForm(
        workflow="de-novo-design",
        tool="bindcraft",
        runName="queued-bindflow-run",
        paramsText="custom_param: value",
    )
    form_data = WorkflowFormData(
        workflow="de-novo-design",
        tool="bindcraft",
        number_of_final_designs=3,
    )

    with (
        patch("app.services.bindflow_executor.get_bindflow_config_profiles", return_value=["gadi"]),
        patch(
            "app.services.bindflow_executor.get_bindflow_config_text", return_value="config_text"
        ),
    ):
        prepared_job = await prepare_bindflow_workflow(
            form=form,
            s3_input_key="inputs/samplesheets/test.csv",
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            revision="main",
            output_id="run-output-id",
            mode="bindcraft",
            form_data=form_data,
            user_details=_USER_DETAILS,
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
    assert queued_job.launch_payload["runName"] == "queued-bindflow-run"
    assert queued_job.launch_payload["pipeline"] == "https://github.com/test/repo"
    assert queued_job.launch_payload["workDir"] == "/work/dir"
    assert queued_job.launch_payload["workspaceId"] == "ws_123"
    assert queued_job.launch_payload["revision"] == "main"
    assert queued_job.launch_payload["configProfiles"] == ["gadi"]
    assert queued_job.launch_payload["configText"] == "config_text"
    assert "preRunScript" not in queued_job.launch_payload
    assert queued_job.launch_payload["resume"] is False
    assert "outdir: s3://my-bucket/run-output-id" in queued_job.launch_payload["paramsText"]
    assert (
        "input: s3://my-bucket/inputs/samplesheets/test.csv"
        in queued_job.launch_payload["paramsText"]
    )
    assert "mode: bindcraft" in queued_job.launch_payload["paramsText"]
    assert "number_of_final_designs: 3" in queued_job.launch_payload["paramsText"]
    assert "custom_param: value" in queued_job.launch_payload["paramsText"]


@pytest.mark.asyncio
@respx.mock
async def test_launch_success_with_all_params(persistent_models):
    """Test successful launch with all parameters."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_full_456"},
        )
    )

    with (
        _mock_bindflow_db_context() as (db_session, workflow_run, *_),
        patch(
            "app.services.bindflow_executor.get_executor_script",
            return_value="prerun_body",
        ) as mock_script,
    ):
        result = await launch_bindflow_workflow(
            queued_job=_queued_bindflow_job(
                params_text=(
                    "input: s3://test-s3-bucket/inputs/samplesheets/test.csv\n"
                    "custom_param: value"
                ),
                prerun_script_path="/some/prerun.sh",
            )
        )

    assert result.workflow_id == "wf_full_456"
    assert route.called
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert (
        "input: s3://test-s3-bucket/inputs/samplesheets/test.csv" in payload["launch"]["paramsText"]
    )
    assert payload["launch"]["preRunScript"] == "prerun_body"
    assert mock_script.call_args.kwargs["prerun_script_path"] == "/some/prerun.sh"


@pytest.mark.asyncio
@respx.mock
async def test_launch_includes_default_params(persistent_models):
    """Test that default parameters are included."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_123"})
    )

    await launch_bindflow_workflow(queued_job=_queued_bindflow_job())

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "project: yz52" in params_text
    assert "outdir:" in params_text
    assert "input:" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_dataset_adds_input_url(persistent_models):
    """Test that providing a dataset ID adds it to launch payload."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_dataset_999"})
    )

    await launch_bindflow_workflow(queued_job=_queued_bindflow_job())

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "input: s3://test-s3-bucket/inputs/samplesheets/test.csv" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_api_error_response(persistent_models):
    """Test handling of API error response."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(400, text="Invalid request")
    )

    with pytest.raises(WorkflowExecutorError, match="400"):
        await launch_bindflow_workflow(queued_job=_queued_bindflow_job())


@pytest.mark.asyncio
@respx.mock
async def test_launch_missing_workflow_id_in_response(persistent_models):
    """Test error handling when API response lacks workflowId."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    with pytest.raises(WorkflowExecutorError, match="workflowId"):
        await launch_bindflow_workflow(queued_job=_queued_bindflow_job())


def test_launch_missing_env_vars(persistent_models):
    """Test that missing environment variables raise error."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("SEQERA_API_URL", raising=False)
        monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("WORK_SPACE", raising=False)

        with pytest.raises(SeqeraConfigurationError):
            asyncio.run(launch_bindflow_workflow(queued_job=_queued_bindflow_job()))


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_custom_params_text(persistent_models):
    """Test launch with custom paramsText."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_params_xyz"})
    )

    await launch_bindflow_workflow(
        queued_job=_queued_bindflow_job(
            params_text=(
                "outdir: s3://test-s3-bucket/run-out\n"
                "input: s3://test-s3-bucket/inputs/samplesheets/test.csv\n"
                "my_custom_param: 42\n"
                "another_param: test"
            )
        )
    )

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "my_custom_param: 42" in params_text
    assert "another_param: test" in params_text
