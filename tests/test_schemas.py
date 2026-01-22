"""Tests for Pydantic schemas."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.workflows import (
    CancelWorkflowResponse,
    DatasetUploadRequest,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    RunInfo,
    WorkflowLaunchForm,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
)


def test_valid_minimal_form():
    """Test WorkflowLaunchForm with minimal valid data."""
    form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

    assert form.pipeline == "https://github.com/test/repo"
    assert form.revision is None
    assert form.configProfiles == []
    assert form.runName is None
    assert form.paramsText is None


def test_valid_complete_form():
    """Test WorkflowLaunchForm with all fields."""
    form = WorkflowLaunchForm(
        pipeline="https://github.com/test/repo",
        revision="main",
        configProfiles=["docker", "test"],
        runName="my-test-run",
        paramsText="param1: value1\nparam2: value2",
    )

    assert form.pipeline == "https://github.com/test/repo"
    assert form.revision == "main"
    assert form.configProfiles == ["docker", "test"]
    assert form.runName == "my-test-run"
    assert "param1" in form.paramsText


def test_pipeline_required():
    """Test that pipeline field is required."""
    with pytest.raises(ValidationError) as exc_info:
        WorkflowLaunchForm()

    errors = exc_info.value.errors()
    assert any(error["loc"] == ("pipeline",) for error in errors)


def test_pipeline_cannot_be_empty():
    """Test that pipeline cannot be empty string."""
    with pytest.raises(ValidationError, match="pipeline is required"):
        WorkflowLaunchForm(pipeline="")


def test_pipeline_whitespace_stripped():
    """Test that pipeline whitespace is stripped."""
    form = WorkflowLaunchForm(pipeline="  https://github.com/test/repo  ")
    assert form.pipeline == "https://github.com/test/repo"


def test_extra_fields_forbidden():
    """Test that extra fields are not allowed."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(pipeline="https://github.com/test/repo", extraField="not allowed")


def test_valid_payload_with_launch_only():
    """Test payload with only launch data."""
    payload = WorkflowLaunchPayload(launch={"pipeline": "https://github.com/test/repo"})

    assert payload.launch.pipeline == "https://github.com/test/repo"
    assert payload.datasetId is None
    assert payload.formData is None


def test_valid_payload_with_dataset_id():
    """Test payload with dataset ID."""
    payload = WorkflowLaunchPayload(
        launch={"pipeline": "https://github.com/test/repo"},
        datasetId="dataset_123",
    )

    assert payload.datasetId == "dataset_123"


def test_valid_payload_with_form_data():
    """Test payload with form data."""
    form_data = {
        "sample": "test",
        "input": "/path/to/file",
        "param": 42,
    }
    payload = WorkflowLaunchPayload(
        launch={"pipeline": "https://github.com/test/repo"},
        formData=form_data,
    )

    assert payload.formData == form_data


def test_payload_extra_fields_forbidden():
    """Test that extra fields are not allowed in payload."""
    with pytest.raises(ValidationError):
        WorkflowLaunchPayload(
            launch={"pipeline": "https://github.com/test/repo"}, unknownField="value"
        )


def test_valid_response():
    """Test creating a valid launch response."""
    response = WorkflowLaunchResponse(
        message="Workflow launched",
        runId="run_123",
        status="submitted",
        submitTime=datetime(2024, 1, 1, 12, 0, 0),
    )

    assert response.message == "Workflow launched"
    assert response.runId == "run_123"
    assert response.status == "submitted"
    assert response.submitTime.year == 2024


def test_valid_cancel_response():
    """Test creating a valid cancel response."""
    response = CancelWorkflowResponse(
        message="Cancelled",
        runId="run_123",
        status="cancelled",
    )

    assert response.message == "Cancelled"
    assert response.runId == "run_123"
    assert response.status == "cancelled"


def test_valid_run_info():
    """Test creating valid run info."""
    run_info = RunInfo(
        id="run_123",
        run="test-run",
        workflow="test-workflow",
        status="running",
        date="2024-01-01",
        cancel="false",
    )

    assert run_info.id == "run_123"
    assert run_info.status == "running"


def test_empty_runs_list():
    """Test response with empty runs list."""
    response = ListRunsResponse(
        runs=[],
        total=0,
        limit=50,
        offset=0,
    )

    assert response.runs == []
    assert response.total == 0


def test_runs_list_with_data():
    """Test response with run data."""
    run_info = RunInfo(
        id="run_123",
        run="test",
        workflow="wf",
        status="done",
        date="2024-01-01",
        cancel="false",
    )
    response = ListRunsResponse(
        runs=[run_info],
        total=1,
        limit=50,
        offset=0,
    )

    assert len(response.runs) == 1
    assert response.total == 1


def test_valid_logs():
    """Test creating valid launch logs."""
    logs = LaunchLogs(
        truncated=False,
        entries=["log line 1", "log line 2"],
        rewindToken="token1",
        forwardToken="token2",
        pending=False,
        message="Logs retrieved",
    )

    assert len(logs.entries) == 2
    assert logs.truncated is False


def test_valid_details():
    """Test creating valid launch details."""
    details = LaunchDetails(
        requiresAttention=False,
        status="completed",
        ownerId=123,
        repository="https://github.com/test/repo",
        id="launch_123",
        submit="2024-01-01T12:00:00",
        start="2024-01-01T12:01:00",
        complete="2024-01-01T12:10:00",
        dateCreated="2024-01-01T12:00:00",
        lastUpdated="2024-01-01T12:10:00",
        runName="test-run",
        sessionId="session_123",
        profile="standard",
        workDir="/work",
        commitId="abc123",
        userName="testuser",
        scriptId="script_123",
        revision="main",
        commandLine="nextflow run",
        projectName="test-project",
        scriptName="main.nf",
        launchId="launch_123",
        configFiles=["nextflow.config"],
        params={"test": "value"},
    )

    assert details.status == "completed"
    assert details.ownerId == 123


def test_dataset_upload_request_valid():
    """Test creating valid DatasetUploadRequest."""
    request = DatasetUploadRequest(
        formData={"sample": "test", "input": "/path/file"},
        datasetName="test-dataset",
        datasetDescription="Test description",
    )

    assert request.formData == {"sample": "test", "input": "/path/file"}
    assert request.datasetName == "test-dataset"
    assert request.datasetDescription == "Test description"


def test_dataset_upload_request_empty_form_data():
    """Test DatasetUploadRequest validator rejects empty formData."""
    with pytest.raises(ValidationError, match="formData cannot be empty"):
        DatasetUploadRequest(
            formData={},
            datasetName="test-dataset",
        )
