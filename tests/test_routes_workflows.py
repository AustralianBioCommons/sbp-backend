"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from app.routes.dependencies import get_current_user_id, get_db
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
    client.app.dependency_overrides[get_current_user_id] = lambda: UUID(
        "11111111-1111-1111-1111-111111111111"
    )
    client.app.dependency_overrides[get_db] = lambda: iter([None])
    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=object()),
        patch(
            "app.routes.workflow.jobs.cancel_seqera_workflow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post("/api/workflows/run_123/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["runId"] == "run_123"
    assert data["status"] == "cancelled"
    assert "message" in data


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
