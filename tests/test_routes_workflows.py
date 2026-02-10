"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
)
from app.services.seqera_errors import SeqeraAPIError


@patch("app.routes.workflows.get_owned_run_ids")
@patch("app.routes.workflows.get_score_by_seqera_run_id")
@patch("app.routes.workflows.get_workflow_type_by_seqera_run_id")
@patch("app.routes.workflows.describe_workflow", new_callable=AsyncMock)
@patch("app.routes.workflows.get_owned_run")
@patch("app.routes.workflows.ensure_completed_run_score", new_callable=AsyncMock)
async def test_list_jobs_success(
    mock_ensure_score,
    mock_get_owned_run,
    mock_describe,
    mock_types,
    mock_scores,
    mock_owned_ids,
):
    """Test job listing via workflows router."""
    run_id = "wf-111"
    mock_owned_ids.return_value = [run_id]
    mock_scores.return_value = {}
    mock_types.return_value = {run_id: "BindCraft"}
    mock_describe.return_value = {
        "workflow": {
            "id": run_id,
            "runName": "Test Job",
            "status": "SUCCEEDED",
            "submit": "2026-02-01T10:00:00Z",
        }
    }
    mock_get_owned_run.return_value = object()
    mock_ensure_score.return_value = 0.42

    from app.routes.workflows import list_jobs

    response = await list_jobs(
        search=None,
        status_filter=None,
        limit=50,
        offset=0,
        current_user_id="user",
        db=object(),
    )

    assert response.total == 1
    assert response.jobs[0].id == run_id
    assert response.jobs[0].score == 0.42


@patch("app.routes.workflows.describe_workflow", new_callable=AsyncMock)
async def test_list_jobs_seqera_error_maps_502(mock_describe):
    """Test Seqera API error maps to HTTP 502."""
    mock_describe.side_effect = SeqeraAPIError("boom")

    from app.routes.workflows import list_jobs

    with patch("app.routes.workflows.get_owned_run_ids", return_value=["wf-1"]), patch(
        "app.routes.workflows.get_score_by_seqera_run_id", return_value={}
    ), patch("app.routes.workflows.get_workflow_type_by_seqera_run_id", return_value={}):
        with pytest.raises(HTTPException) as exc:
            await list_jobs(
                search=None,
                status_filter=None,
                limit=50,
                offset=0,
                current_user_id="user",
                db=object(),
            )
    assert exc.value.status_code == 502


def test_list_runs_placeholder(client: TestClient):
    """Test list runs placeholder endpoint."""
    response = client.get("/api/workflows/runs?limit=10&offset=5")

    assert response.status_code == 200
    data = response.json()
    assert data["runs"] == []
    assert data["limit"] == 10
    assert data["offset"] == 5


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


def test_cancel_workflow_endpoint_removed(client: TestClient):
    """Cancel endpoint is intentionally removed from jobs API."""
    response = client.post("/api/workflows/run_123/cancel")
    assert response.status_code == 404


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
