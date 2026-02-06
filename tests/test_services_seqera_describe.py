"""Additional tests for Seqera service describe_workflow function."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.seqera import (
    SeqeraAPIError,
    SeqeraConfigurationError,
    describe_workflow,
)


@pytest.mark.asyncio
async def test_describe_workflow_success(monkeypatch):
    """Test successful workflow description."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = {
        "workflow": {
            "id": "wf-123",
            "runName": "Test Workflow",
            "status": "SUCCEEDED",
        }
    }

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await describe_workflow("wf-123")

    assert result["workflow"]["id"] == "wf-123"
    assert result["workflow"]["runName"] == "Test Workflow"


@pytest.mark.asyncio
async def test_describe_workflow_with_custom_workspace(monkeypatch):
    """Test workflow description with custom workspace."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = {"workflow": {"id": "wf-456"}}

    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        await describe_workflow("wf-456", workspace_id="custom-workspace")

    # Verify that custom workspace was used
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["workspaceId"] == "custom-workspace"


@pytest.mark.asyncio
async def test_describe_workflow_missing_api_url(monkeypatch):
    """Test error when SEQERA_API_URL is missing."""
    monkeypatch.delenv("SEQERA_API_URL", raising=False)
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    with pytest.raises(SeqeraConfigurationError) as exc_info:
        await describe_workflow("wf-123")

    assert "SEQERA_API_URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_missing_access_token(monkeypatch):
    """Test error when SEQERA_ACCESS_TOKEN is missing."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    with pytest.raises(SeqeraConfigurationError) as exc_info:
        await describe_workflow("wf-123")

    assert "SEQERA_ACCESS_TOKEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_missing_workspace(monkeypatch):
    """Test error when WORK_SPACE is missing."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("WORK_SPACE", raising=False)

    with pytest.raises(SeqeraConfigurationError) as exc_info:
        await describe_workflow("wf-123")

    assert "WORK_SPACE" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_api_error_404(monkeypatch):
    """Test API error response with 404."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.text = "Workflow not found"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError) as exc_info:
            await describe_workflow("nonexistent")

    assert "404" in str(exc_info.value)
    assert "Workflow not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_api_error_500(monkeypatch):
    """Test API error response with 500."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 500
    mock_response.reason_phrase = "Internal Server Error"
    mock_response.text = "Server error occurred"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError) as exc_info:
            await describe_workflow("wf-error")

    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_strips_trailing_slash(monkeypatch):
    """Test that trailing slash in API URL is stripped."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test/")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = {"workflow": {"id": "wf-789"}}

    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        await describe_workflow("wf-789")

    # Verify URL doesn't have double slashes
    call_args = mock_get.call_args.args
    assert "//workflow" not in call_args[0]
    assert "/workflow/wf-789" in call_args[0]
