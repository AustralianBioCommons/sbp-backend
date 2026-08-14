"""Additional tests for Seqera service describe_workflow function."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.seqera import (
    SeqeraAPIError,
    describe_workflow,
)


@pytest.mark.asyncio
async def test_describe_workflow_success(mock_settings):
    """Test successful workflow description."""
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "test-token"
    mock_settings.seqera.work_space = "test-workspace"

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
        result = await describe_workflow("wf-123", settings=mock_settings)

    assert result["workflow"]["id"] == "wf-123"
    assert result["workflow"]["runName"] == "Test Workflow"


@pytest.mark.asyncio
async def test_describe_workflow_with_custom_workspace(mock_settings):
    """Test workflow description with custom workspace."""
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "test-token"

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = {"workflow": {"id": "wf-456"}}

    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        await describe_workflow("wf-456", workspace_id="custom-workspace", settings=mock_settings)

    # Verify that custom workspace was used
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["workspaceId"] == "custom-workspace"


@pytest.mark.asyncio
async def test_describe_workflow_api_error_404(mock_settings):
    """Test API error response with 404."""
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "test-token"
    mock_settings.seqera.work_space = "test-workspace"

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.text = "Workflow not found"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError) as exc_info:
            await describe_workflow("nonexistent", settings=mock_settings)

    assert "404" in str(exc_info.value)
    assert "Workflow not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_api_error_500(mock_settings):
    """Test API error response with 500."""
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "test-token"
    mock_settings.seqera.work_space = "test-workspace"

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = True
    mock_response.status_code = 500
    mock_response.reason_phrase = "Internal Server Error"
    mock_response.text = "Server error occurred"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        with pytest.raises(SeqeraAPIError) as exc_info:
            await describe_workflow("wf-error", settings=mock_settings)

    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_describe_workflow_strips_trailing_slash(mock_settings):
    """Test that trailing slash in API URL is stripped."""
    mock_settings.seqera.api_url = "https://api.seqera.test/"
    mock_settings.seqera.access_token = "test-token"
    mock_settings.seqera.work_space = "test-workspace"

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.is_error = False
    mock_response.json.return_value = {"workflow": {"id": "wf-789"}}

    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        await describe_workflow("wf-789", settings=mock_settings)

    # Verify URL doesn't have double slashes
    call_args = mock_get.call_args.args
    assert "//workflow" not in call_args[0]
    assert "/workflow/wf-789" in call_args[0]
