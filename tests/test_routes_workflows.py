"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.core import RunMetric, WorkflowRun
from app.services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
)


def test_list_runs_placeholder(client: TestClient):
    """Test list runs placeholder endpoint."""
    response = client.get("/api/workflows/runs?limit=10&offset=5")

    assert response.status_code == 200
    data = response.json()
    assert data["runs"] == []
    assert data["limit"] == 10
    assert data["offset"] == 5


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_without_dataset(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch without dataset."""
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_123",
        status="submitted",
        message="Success",
    )

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"id": "PDL1", "number_of_final_designs": 20},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_123"
    assert data["status"] == "submitted"
    assert "submitTime" in data
    launch_form_arg = mock_launch.call_args[0][0]
    assert launch_form_arg.tool == "BindCraft"
    assert mock_launch.call_args.kwargs["pipeline"] == "https://github.com/test/repo"
    assert mock_launch.call_args.kwargs["revision"] == "dev"
    assert isinstance(mock_launch.call_args.kwargs["output_id"], str)

    with Session(test_engine) as db:
        created_run = db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.seqera_dataset_id,
                WorkflowRun.run_name,
                WorkflowRun.binder_name,
                WorkflowRun.sample_id,
            ).where(WorkflowRun.seqera_run_id == "wf_123")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_123"
        assert created_run.run_name == "test-run"
        assert created_run.binder_name == "PDL1"
        assert created_run.sample_id == "PDL1"
        metric = db.execute(select(RunMetric).where(RunMetric.run_id == created_run.id)).scalar_one()
        assert metric.final_design_count == 20


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_with_dataset_id(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch with pre-created dataset ID."""
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_789",
        status="submitted",
    )

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-with-data",
        },
        "datasetId": "dataset_456",  # Use existing dataset
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_789"

    mock_launch.assert_called_once()
    call_args = mock_launch.call_args
    assert call_args[0][1] == "dataset_456"

    with Session(test_engine) as db:
        created_run = db.execute(
            select(WorkflowRun.seqera_dataset_id).where(WorkflowRun.seqera_run_id == "wf_789")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_456"


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_configuration_error(mock_launch, client: TestClient, test_engine):
    """Test launch with configuration error."""
    mock_launch.side_effect = BindflowConfigurationError("Missing API token")

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "Missing API token" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 1


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_service_error(mock_launch, client: TestClient, test_engine):
    """Test launch with Seqera service error."""
    mock_launch.side_effect = BindflowExecutorError("API returned 502")

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 502
    assert "API returned 502" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 1


def test_launch_invalid_payload(client: TestClient):
    """Test launch with invalid payload."""
    payload = {
        "launch": {},
        "unknownField": "not allowed",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422  # Validation error


def test_cancel_workflow_endpoint_removed(client: TestClient):
    """Cancel endpoint is intentionally removed from jobs API."""
    response = client.post("/api/workflows/run_123/cancel")
    assert response.status_code == 404


def test_launch_rejects_unavailable_tool(client: TestClient):
    payload = {
        "launch": {
            "tool": "BoltzGen",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 501
    assert "not available" in response.json()["detail"]


def test_launch_rejects_unknown_tool(client: TestClient):
    payload = {
        "launch": {
            "tool": "UnknownTool",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 501
    assert "Only BindCraft is supported" in response.json()["detail"]


def test_get_logs_success(client: TestClient):
    """Test successful log retrieval."""
    response = client.get("/api/workflows/run_123/logs")

    assert response.status_code == 200
    data = response.json()
    assert "entries" in data
    assert "truncated" in data
    assert "pending" in data
    assert isinstance(data["entries"], list)


def test_get_details_success(client: TestClient):
    """Test successful details retrieval."""
    response = client.get("/api/workflows/run_123/details")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "run_123"
    assert "status" in data
    assert "runName" in data
