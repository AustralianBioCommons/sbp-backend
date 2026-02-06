"""Tests for Seqera workflow listing service."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.seqera import (
    SeqeraAPIError,
    SeqeraConfigurationError,
    WorkflowListItem,
    describe_workflow,
    list_seqera_workflows,
)


@pytest.mark.asyncio
async def test_list_seqera_workflows_success(monkeypatch):
    """Test successful workflow listing from Seqera API."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response_data = {
        "workflows": [
            {
                "id": "wf-123",
                "runName": "Test BindCraft Run",
                "projectName": "bindcraft-pipeline",
                "status": "SUCCEEDED",
                "submit": "2026-02-01T10:00:00Z",
            },
            {
                "id": "wf-456",
                "runName": "De novo design test",
                "projectName": "denovo-pipeline",
                "status": "RUNNING",
                "submit": "2026-02-02T11:00:00Z",
            },
        ],
        "totalSize": 2,
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = mock_response_data

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        workflows, total = await list_seqera_workflows(limit=10, offset=0)

    assert total == 2
    assert len(workflows) == 2
    assert workflows[0].workflow_id == "wf-123"
    assert workflows[0].run_name == "Test BindCraft Run"
    assert workflows[0].workflow_type == "BindCraft"
    assert workflows[0].ui_status == "Completed"
    assert workflows[0].pipeline_status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_list_seqera_workflows_with_search(monkeypatch):
    """Test workflow listing with search query."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response_data = {
        "workflows": [
            {
                "id": "wf-789",
                "runName": "Matching Job",
                "projectName": "test-pipeline",
                "status": "FAILED",
                "submit": "2026-02-03T12:00:00Z",
            }
        ],
        "totalSize": 1,
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = mock_response_data

    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        workflows, total = await list_seqera_workflows(search_query="Matching", limit=10, offset=0)

    # Verify search parameter was passed
    call_args = mock_get.call_args
    assert call_args.kwargs["params"]["search"] == "Matching"

    assert total == 1
    assert len(workflows) == 1
    assert workflows[0].run_name == "Matching Job"
    assert workflows[0].ui_status == "Failed"


@pytest.mark.asyncio
async def test_list_seqera_workflows_with_status_filter(monkeypatch):
    """Test workflow listing with status filter."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response_data = {
        "workflows": [
            {
                "id": "wf-100",
                "runName": "Completed Job",
                "status": "SUCCEEDED",
                "submit": "2026-02-01T10:00:00Z",
            },
            {
                "id": "wf-101",
                "runName": "Failed Job",
                "status": "FAILED",
                "submit": "2026-02-02T11:00:00Z",
            },
        ],
        "totalSize": 2,
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = mock_response_data

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        workflows, total = await list_seqera_workflows(
            status_filter=["Completed"], limit=10, offset=0
        )

    # Only completed workflows should be returned
    assert len(workflows) == 1
    assert workflows[0].ui_status == "Completed"
    assert workflows[0].workflow_id == "wf-100"


@pytest.mark.asyncio
async def test_list_seqera_workflows_status_mapping(monkeypatch):
    """Test that all pipeline statuses are correctly mapped to UI statuses."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response_data = {
        "workflows": [
            {"id": "wf-1", "runName": "Job 1", "status": "SUBMITTED"},
            {"id": "wf-2", "runName": "Job 2", "status": "RUNNING"},
            {"id": "wf-3", "runName": "Job 3", "status": "SUCCEEDED"},
            {"id": "wf-4", "runName": "Job 4", "status": "FAILED"},
            {"id": "wf-5", "runName": "Job 5", "status": "CANCELLED"},
            {"id": "wf-6", "runName": "Job 6", "status": "UNKNOWN"},
        ],
        "totalSize": 6,
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = mock_response_data

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        workflows, total = await list_seqera_workflows(limit=10, offset=0)

    assert len(workflows) == 6

    status_mapping = {
        "SUBMITTED": "In queue",
        "RUNNING": "In progress",
        "SUCCEEDED": "Completed",
        "FAILED": "Failed",
        "CANCELLED": "Stopped",
        "UNKNOWN": "Failed",
    }

    for wf in workflows:
        expected_ui_status = status_mapping[wf.pipeline_status]
        assert wf.ui_status == expected_ui_status


@pytest.mark.asyncio
async def test_list_seqera_workflows_missing_config(monkeypatch):
    """Test that missing configuration raises appropriate error."""
    monkeypatch.delenv("SEQERA_API_URL", raising=False)

    with pytest.raises(SeqeraConfigurationError, match="SEQERA_API_URL"):
        await list_seqera_workflows()


@pytest.mark.asyncio
async def test_list_seqera_workflows_api_error(monkeypatch):
    """Test handling of Seqera API errors."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 500
    mock_response.reason_phrase = "Internal Server Error"
    mock_response.text = "Server error occurred"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError, match="Failed to list workflows"):
            await list_seqera_workflows()


@pytest.mark.asyncio
async def test_describe_workflow_success(monkeypatch):
    """Test describing a specific workflow."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response_data = {
        "workflow": {
            "id": "wf-123",
            "runName": "Test Run",
            "status": "SUCCEEDED",
            "submit": "2026-02-01T10:00:00Z",
            "complete": "2026-02-01T11:30:00Z",
        }
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = mock_response_data

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await describe_workflow("wf-123")

    assert result == mock_response_data


@pytest.mark.asyncio
async def test_describe_workflow_not_found(monkeypatch):
    """Test describing a non-existent workflow."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.text = "Workflow not found"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError, match="Failed to describe workflow"):
            await describe_workflow("wf-nonexistent")
