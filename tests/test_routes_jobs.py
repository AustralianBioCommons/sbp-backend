"""Tests for job listing API endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.seqera import WorkflowListItem


@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


def test_list_jobs_endpoint_success(client, monkeypatch):
    """Test successful job listing via API endpoint."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_workflows = [
        WorkflowListItem(
            workflow_id="wf-123",
            run_name="Test Job 1",
            workflow_type="BindCraft",
            pipeline_status="SUCCEEDED",
            ui_status="Completed",
            submitted_at=datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
            score=0.95,
        ),
        WorkflowListItem(
            workflow_id="wf-456",
            run_name="Test Job 2",
            workflow_type="De novo design",
            pipeline_status="RUNNING",
            ui_status="In progress",
            submitted_at=datetime(2026, 2, 2, 11, 0, 0, tzinfo=timezone.utc),
            score=None,
        ),
    ]

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        return_value=(mock_workflows, 2),
    ):
        response = client.get("/jobs?limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 2
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert len(data["jobs"]) == 2

    job1 = data["jobs"][0]
    assert job1["id"] == "wf-123"
    assert job1["jobName"] == "Test Job 1"
    assert job1["workflowType"] == "BindCraft"
    assert job1["status"] == "Completed"
    assert job1["score"] == 0.95

    job2 = data["jobs"][1]
    assert job2["id"] == "wf-456"
    assert job2["jobName"] == "Test Job 2"
    assert job2["workflowType"] == "De novo design"
    assert job2["status"] == "In progress"
    assert job2["score"] is None


def test_list_jobs_with_search_query(client, monkeypatch):
    """Test job listing with search query."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_workflows = [
        WorkflowListItem(
            workflow_id="wf-789",
            run_name="Matching Job",
            workflow_type="BindCraft",
            pipeline_status="SUCCEEDED",
            ui_status="Completed",
            submitted_at=datetime(2026, 2, 3, 12, 0, 0, tzinfo=timezone.utc),
            score=None,
        )
    ]

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        return_value=(mock_workflows, 1),
    ) as mock_list:
        response = client.get("/jobs?search=Matching&limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()

    # Verify the service was called with search parameter
    mock_list.assert_called_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["search_query"] == "Matching"

    assert data["total"] == 1
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["jobName"] == "Matching Job"


def test_list_jobs_with_status_filter(client, monkeypatch):
    """Test job listing with status filter."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_workflows = [
        WorkflowListItem(
            workflow_id="wf-100",
            run_name="Completed Job",
            workflow_type="BindCraft",
            pipeline_status="SUCCEEDED",
            ui_status="Completed",
            submitted_at=datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
            score=None,
        )
    ]

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        return_value=(mock_workflows, 1),
    ) as mock_list:
        response = client.get("/jobs?status=Completed&status=Failed&limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()

    # Verify the service was called with status filter
    mock_list.assert_called_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["status_filter"] == ["Completed", "Failed"]

    assert data["total"] == 1
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["status"] == "Completed"


def test_list_jobs_with_pagination(client, monkeypatch):
    """Test job listing with pagination parameters."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_workflows = [
        WorkflowListItem(
            workflow_id=f"wf-{i}",
            run_name=f"Job {i}",
            workflow_type="BindCraft",
            pipeline_status="SUCCEEDED",
            ui_status="Completed",
            submitted_at=datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
            score=None,
        )
        for i in range(20, 40)
    ]

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        return_value=(mock_workflows, 100),
    ) as mock_list:
        response = client.get("/jobs?limit=20&offset=20")

    assert response.status_code == 200
    data = response.json()

    # Verify the service was called with correct pagination
    mock_list.assert_called_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["limit"] == 20
    assert call_kwargs["offset"] == 20

    assert data["total"] == 100
    assert data["limit"] == 20
    assert data["offset"] == 20
    assert len(data["jobs"]) == 20


def test_list_jobs_empty_results(client, monkeypatch):
    """Test job listing with no results."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        response = client.get("/jobs?limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 0
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert len(data["jobs"]) == 0


def test_list_jobs_invalid_limit(client, monkeypatch):
    """Test job listing with invalid limit parameter."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    response = client.get("/jobs?limit=0")
    assert response.status_code == 422  # Validation error

    response = client.get("/jobs?limit=500")
    assert response.status_code == 422  # Exceeds max limit


def test_list_jobs_invalid_offset(client, monkeypatch):
    """Test job listing with invalid offset parameter."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    response = client.get("/jobs?offset=-1")
    assert response.status_code == 422  # Validation error


def test_list_jobs_configuration_error(client, monkeypatch):
    """Test job listing with missing configuration."""
    # Don't set required environment variables
    monkeypatch.delenv("SEQERA_API_URL", raising=False)

    response = client.get("/jobs?limit=10&offset=0")
    assert response.status_code == 500
    assert "SEQERA_API_URL" in response.json()["detail"]


def test_list_jobs_api_error(client, monkeypatch):
    """Test job listing when Seqera API fails."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    from app.services.seqera import SeqeraAPIError

    with patch(
        "app.routes.workflows.list_seqera_workflows",
        new_callable=AsyncMock,
        side_effect=SeqeraAPIError("Seqera API is down"),
    ):
        response = client.get("/jobs?limit=10&offset=0")

    assert response.status_code == 502
    assert "Seqera API is down" in response.json()["detail"]
