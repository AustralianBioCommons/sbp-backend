"""Tests for Pydantic schemas."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.workflows import (
    CancelWorkflowResponse,
    DatasetUploadRequest,
    JobListItem,
    JobListResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    PipelineStatus,
    RunInfo,
    UIStatus,
    WorkflowLaunchForm,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
    map_pipeline_status_to_ui,
)


def test_valid_minimal_form():
    """Test WorkflowLaunchForm with minimal valid data."""
    form = WorkflowLaunchForm(tool="BindCraft")

    assert form.tool == "BindCraft"
    assert form.configProfiles == []
    assert form.runName is None
    assert form.paramsText is None


def test_valid_complete_form():
    """Test WorkflowLaunchForm with all fields."""
    form = WorkflowLaunchForm(
        tool="BindCraft",
        configProfiles=["docker", "test"],
        runName="my-test-run",
        paramsText="param1: value1\nparam2: value2",
    )

    assert form.tool == "BindCraft"
    assert form.configProfiles == ["docker", "test"]
    assert form.runName == "my-test-run"
    assert "param1" in form.paramsText


def test_tool_required_and_not_empty():
    """Test that tool is required and cannot be empty."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm()
    with pytest.raises(ValidationError, match="tool is required"):
        WorkflowLaunchForm(tool="")


def test_tool_whitespace_stripped():
    """Test that tool whitespace is stripped."""
    form = WorkflowLaunchForm(tool="  BindCraft  ")
    assert form.tool == "BindCraft"


def test_extra_fields_forbidden():
    """Test that extra fields are not allowed."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(tool="BindCraft", extraField="not allowed")


def test_valid_payload_with_launch_only():
    """Test payload with required launch and dataset ID."""
    payload = WorkflowLaunchPayload(launch={"tool": "BindCraft"}, datasetId="dataset_123")

    assert payload.launch.tool == "BindCraft"
    assert payload.datasetId == "dataset_123"
    assert payload.formData is None


def test_valid_payload_with_dataset_id():
    """Test payload with dataset ID."""
    payload = WorkflowLaunchPayload(
        launch={"tool": "BindCraft"},
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
        launch={"tool": "BindCraft"},
        formData=form_data,
        datasetId="dataset_123",
    )

    assert payload.formData == form_data


def test_payload_extra_fields_forbidden():
    """Test that extra fields are not allowed in payload."""
    with pytest.raises(ValidationError):
        WorkflowLaunchPayload(launch={"tool": "BindCraft"}, unknownField="value")


def test_payload_requires_dataset_id():
    with pytest.raises(ValidationError):
        WorkflowLaunchPayload(launch={"tool": "BindCraft"})


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


# Tests for Job Listing schemas


def test_status_mapping_submitted():
    """Test pipeline status SUBMITTED maps to 'In queue'."""
    assert map_pipeline_status_to_ui("SUBMITTED") == "In queue"
    assert map_pipeline_status_to_ui(PipelineStatus.SUBMITTED.value) == UIStatus.IN_QUEUE.value


def test_status_mapping_running():
    """Test pipeline status RUNNING maps to 'In progress'."""
    assert map_pipeline_status_to_ui("RUNNING") == "In progress"
    assert map_pipeline_status_to_ui(PipelineStatus.RUNNING.value) == UIStatus.IN_PROGRESS.value


def test_status_mapping_succeeded():
    """Test pipeline status SUCCEEDED maps to 'Completed'."""
    assert map_pipeline_status_to_ui("SUCCEEDED") == "Completed"
    assert map_pipeline_status_to_ui(PipelineStatus.SUCCEEDED.value) == UIStatus.COMPLETED.value


def test_status_mapping_failed():
    """Test pipeline status FAILED maps to 'Failed'."""
    assert map_pipeline_status_to_ui("FAILED") == "Failed"
    assert map_pipeline_status_to_ui(PipelineStatus.FAILED.value) == UIStatus.FAILED.value


def test_status_mapping_unknown():
    """Test pipeline status UNKNOWN maps to 'Failed'."""
    assert map_pipeline_status_to_ui("UNKNOWN") == "Failed"
    assert map_pipeline_status_to_ui(PipelineStatus.UNKNOWN.value) == UIStatus.FAILED.value


def test_status_mapping_cancelled():
    """Test pipeline status CANCELLED maps to 'Stopped'."""
    assert map_pipeline_status_to_ui("CANCELLED") == "Stopped"
    assert map_pipeline_status_to_ui(PipelineStatus.CANCELLED.value) == UIStatus.STOPPED.value


def test_status_mapping_invalid():
    """Test invalid pipeline status defaults to 'Failed'."""
    assert map_pipeline_status_to_ui("INVALID_STATUS") == "Failed"
    assert map_pipeline_status_to_ui("") == "Failed"


def test_job_list_item_valid():
    """Test creating valid JobListItem."""
    job = JobListItem(
        id="wf-123",
        jobName="Test Job",
        workflowType="BindCraft",
        status="Completed",
        submittedAt=datetime(2026, 2, 1, 10, 0, 0),
        score=0.95,
    )

    assert job.id == "wf-123"
    assert job.jobName == "Test Job"
    assert job.workflowType == "BindCraft"
    assert job.status == "Completed"
    assert job.score == 0.95


def test_job_list_item_optional_fields():
    """Test JobListItem with optional fields as None."""
    job = JobListItem(
        id="wf-456",
        jobName="Another Job",
        workflowType=None,
        status="In progress",
        submittedAt=datetime(2026, 2, 2, 11, 0, 0),
        score=None,
    )

    assert job.workflowType is None
    assert job.score is None


def test_job_list_response_valid():
    """Test creating valid JobListResponse."""
    jobs = [
        JobListItem(
            id="wf-1",
            jobName="Job 1",
            status="Completed",
            submittedAt=datetime(2026, 2, 1, 10, 0, 0),
        ),
        JobListItem(
            id="wf-2",
            jobName="Job 2",
            status="In progress",
            submittedAt=datetime(2026, 2, 2, 11, 0, 0),
        ),
    ]

    response = JobListResponse(
        jobs=jobs,
        total=100,
        limit=10,
        offset=0,
    )

    assert len(response.jobs) == 2
    assert response.total == 100
    assert response.limit == 10
    assert response.offset == 0


def test_job_list_response_empty():
    """Test JobListResponse with empty job list."""
    response = JobListResponse(
        jobs=[],
        total=0,
        limit=10,
        offset=0,
    )

    assert len(response.jobs) == 0
    assert response.total == 0
