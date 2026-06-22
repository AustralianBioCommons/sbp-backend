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
from app.schemas.workflows import WorkflowFormData, WorkflowLaunchForm
from app.services.bindflow_executor import (
    _get_required_env,
    launch_bindflow_workflow,
    prepare_bindflow_workflow,
)
from app.services.seqera import WorkflowExecutorError, WorkflowLaunchResult
from app.services.seqera_errors import SeqeraConfigurationError
from tests.datagen import AppUserFactory, WorkflowFactory, WorkflowRunFactory

_CONFIG_PATH = "/some/bindflow.config"


def _empty_form_data() -> WorkflowFormData:
    return WorkflowFormData(workflow="de-novo-design", tool="bindcraft")


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
async def test_launch_success_minimal():
    """Test successful workflow launch with minimal parameters."""
    route = respx.post("https://api.seqera.test/workflow/launch").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_test_123"},
        )
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-test-minimal"
    )

    with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
        result = await launch_bindflow_workflow(
            form,
            dataset_id="dataset_min_001",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            mode="bindcraft",
            form_data=_empty_form_data(),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
            output_id="run-out-1",
        )

    assert isinstance(result, WorkflowLaunchResult)
    assert result.workflow_id == "wf_test_123"
    assert result.status == "submitted"
    assert route.called
    assert route.call_count == 1


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
        patch(
            "app.services.bindflow_executor.get_bindflow_executor_script",
            return_value="prerun_body",
        ),
    ):
        launch_payload = await prepare_bindflow_workflow(
            form=form,
            dataset_id="dataset_abc",
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            revision="main",
            output_id="run-output-id",
            mode="bindcraft",
            form_data=form_data,
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
        )

    queued_job = test_db.scalar(
        select(QueuedJob).where(QueuedJob.workflow_run_id == workflow_run.id)
    )
    assert queued_job is not None
    assert queued_job.workflow_id == workflow.id
    assert queued_job.workflow_run_id == workflow_run.id
    assert queued_job.status == "pending"
    assert queued_job.next_attempt_at is not None
    assert queued_job.launch_payload == launch_payload
    assert queued_job.launch_payload["computeEnvId"] == "ce_456"
    assert queued_job.launch_payload["runName"] == "queued-bindflow-run"
    assert queued_job.launch_payload["pipeline"] == "https://github.com/test/repo"
    assert queued_job.launch_payload["workDir"] == "/work/dir"
    assert queued_job.launch_payload["workspaceId"] == "ws_123"
    assert queued_job.launch_payload["revision"] == "main"
    assert queued_job.launch_payload["datasetIds"] == ["dataset_abc"]
    assert queued_job.launch_payload["configProfiles"] == ["gadi"]
    assert queued_job.launch_payload["configText"] == "config_text"
    assert queued_job.launch_payload["preRunScript"] == "prerun_body"
    assert queued_job.launch_payload["resume"] is False
    assert "outdir: s3://my-bucket/run-output-id" in queued_job.launch_payload["paramsText"]
    assert (
        "input: https://api.seqera.test/workspaces/ws_123/datasets/dataset_abc/v/1/n/samplesheet.csv"
        in queued_job.launch_payload["paramsText"]
    )
    assert "mode: bindcraft" in queued_job.launch_payload["paramsText"]
    assert "number_of_final_designs: 3" in queued_job.launch_payload["paramsText"]
    assert "custom_param: value" in queued_job.launch_payload["paramsText"]


@pytest.mark.asyncio
@respx.mock
async def test_launch_success_with_all_params():
    """Test successful launch with all parameters."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_full_456"},
        )
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design",
        tool="bindcraft",
        runName="my-custom-run",
        configProfiles=["docker", "test"],
        paramsText="custom_param: value",
    )

    with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
        result = await launch_bindflow_workflow(
            form,
            dataset_id="dataset_789",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            revision="main",
            mode="bindcraft",
            form_data=_empty_form_data(),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
            output_id="run-out-2",
        )

    assert result.workflow_id == "wf_full_456"
    assert route.called
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert "datasetIds" in payload["launch"]
    assert "dataset_789" in payload["launch"]["datasetIds"]


@pytest.mark.asyncio
@respx.mock
async def test_launch_includes_default_params():
    """Test that default parameters are included."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_123"})
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-default-params"
    )

    with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
        await launch_bindflow_workflow(
            form,
            dataset_id="dataset_defaults_001",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            mode="bindcraft",
            form_data=_empty_form_data(),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
            output_id="run-out-3",
        )

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "project: yz52" in params_text
    assert "outdir:" in params_text
    assert "input:" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_dataset_adds_input_url():
    """Test that providing a dataset ID adds it to launch payload."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_dataset_999"})
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-dataset-url"
    )

    with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
        await launch_bindflow_workflow(
            form,
            dataset_id="ds_abc",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            mode="bindcraft",
            form_data=_empty_form_data(),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
            output_id="run-out-4",
        )

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "input:" in params_text
    assert "ds_abc" in params_text
    assert "samplesheet.csv" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_api_error_response():
    """Test handling of API error response."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(400, text="Invalid request")
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-api-error"
    )

    with pytest.raises(WorkflowExecutorError, match="400"):
        with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
            await launch_bindflow_workflow(
                form,
                dataset_id="dataset_error_001",
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline="https://github.com/test/repo",
                config_path=_CONFIG_PATH,
                mode="bindcraft",
                form_data=_empty_form_data(),
                user_email="test@example.com",
                full_name="Test_User",
                institute="example.com",
                ip_address="127.0.0.1",
                output_id="run-out-5",
            )


@pytest.mark.asyncio
@respx.mock
async def test_launch_missing_workflow_id_in_response():
    """Test error handling when API response lacks workflowId."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-missing-workflow-id"
    )

    with pytest.raises(WorkflowExecutorError, match="workflowId"):
        with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
            await launch_bindflow_workflow(
                form,
                dataset_id="dataset_error_002",
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline="https://github.com/test/repo",
                config_path=_CONFIG_PATH,
                mode="bindcraft",
                form_data=_empty_form_data(),
                user_email="test@example.com",
                full_name="Test_User",
                institute="example.com",
                ip_address="127.0.0.1",
                output_id="run-out-6",
            )


def test_launch_missing_env_vars():
    """Test that missing environment variables raise error."""
    form = WorkflowLaunchForm(
        workflow="de-novo-design", tool="bindcraft", runName="seqera-custom-params"
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("SEQERA_API_URL", raising=False)
        monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("WORK_SPACE", raising=False)

        with pytest.raises(SeqeraConfigurationError):
            with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
                asyncio.run(
                    launch_bindflow_workflow(
                        form,
                        dataset_id="dataset_env_001",
                        db_session=db_session,
                        workflow_run=workflow_run,
                        pipeline="https://github.com/test/repo",
                        config_path=_CONFIG_PATH,
                        mode="bindcraft",
                        form_data=_empty_form_data(),
                        user_email="test@example.com",
                        full_name="Test_User",
                        institute="example.com",
                        ip_address="127.0.0.1",
                        output_id="run-out-7",
                    )
                )


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_custom_params_text():
    """Test launch with custom paramsText."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_params_xyz"})
    )

    form = WorkflowLaunchForm(
        workflow="de-novo-design",
        tool="bindcraft",
        runName="seqera-custom-params",
        paramsText="my_custom_param: 42\nanother_param: test",
    )

    with _mock_bindflow_db_context() as (db_session, workflow_run, *_):
        await launch_bindflow_workflow(
            form,
            dataset_id="dataset_params_001",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/test/repo",
            config_path=_CONFIG_PATH,
            mode="bindcraft",
            form_data=_empty_form_data(),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
            output_id="run-out-8",
        )

    request = route.calls.last.request
    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "my_custom_param: 42" in params_text
    assert "another_param: test" in params_text
