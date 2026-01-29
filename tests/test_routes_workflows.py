"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
)


@patch("app.routes.workflows.launch_bindflow_workflow")
async def test_launch_success_without_dataset(mock_launch, client: TestClient):
    """Test successful workflow launch without dataset."""
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_123",
        status="submitted",
        message="Success",
    )

    payload = {
        "launch": {
            "pipeline": "https://github.com/test/repo",
            "runName": "test-run",
        }
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_123"
    assert data["status"] == "submitted"
    assert "submitTime" in data


@patch("app.routes.workflows.launch_bindflow_workflow")
async def test_launch_success_with_dataset_id(mock_launch, client: TestClient):
    """Test successful workflow launch with pre-created dataset ID."""
    # Mock workflow launch
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_789",
        status="submitted",
    )

    payload = {
        "launch": {
            "pipeline": "https://github.com/test/repo",
            "runName": "test-with-data",
        },
        "datasetId": "dataset_456",  # Use existing dataset
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_789"

    # Verify workflow was launched with the provided dataset ID
    mock_launch.assert_called_once()
    call_args = mock_launch.call_args
    assert call_args[0][1] == "dataset_456"  # Second argument is dataset_id


@patch("app.routes.workflows.launch_bindflow_workflow")
async def test_launch_configuration_error(mock_launch, client: TestClient):
    """Test launch with configuration error."""
    mock_launch.side_effect = BindflowConfigurationError("Missing API token")

    payload = {
        "launch": {
            "pipeline": "https://github.com/test/repo",
        }
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "Missing API token" in response.json()["detail"]


@patch("app.routes.workflows.launch_bindflow_workflow")
async def test_launch_service_error(mock_launch, client: TestClient):
    """Test launch with Seqera service error."""
    mock_launch.side_effect = BindflowExecutorError("API returned 502")

    payload = {
        "launch": {
            "pipeline": "https://github.com/test/repo",
        }
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 502
    assert "API returned 502" in response.json()["detail"]


def test_launch_invalid_payload(client: TestClient):
    """Test launch with invalid payload."""
    payload = {
        "launch": {
            "pipeline": "",  # Empty pipeline
        }
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422  # Validation error


def test_cancel_workflow_success(client: TestClient):
    """Test successful workflow cancellation."""
    response = client.post("/api/workflows/run_123/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["runId"] == "run_123"
    assert data["status"] == "cancelled"
    assert "message" in data


def test_list_runs_default_params(client: TestClient):
    """Test listing runs with default parameters."""
    response = client.get("/api/workflows/runs")

    assert response.status_code == 200
    data = response.json()
    assert "runs" in data
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert data["total"] == 0


def test_list_runs_with_filters(client: TestClient):
    """Test listing runs with filter parameters."""
    response = client.get(
        "/api/workflows/runs",
        params={
            "status": "running",
            "workspace": "test_ws",
            "limit": 10,
            "offset": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 10
    assert data["offset"] == 5


def test_list_runs_limit_validation(client: TestClient):
    """Test that limit must be between 1 and 200."""
    # Test limit too high
    response = client.get("/api/workflows/runs", params={"limit": 300})
    assert response.status_code == 422

    # Test limit too low
    response = client.get("/api/workflows/runs", params={"limit": 0})
    assert response.status_code == 422


def test_list_runs_offset_validation(client: TestClient):
    """Test that offset must be non-negative."""
    response = client.get("/api/workflows/runs", params={"offset": -1})
    assert response.status_code == 422


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
