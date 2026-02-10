"""Tests for Seqera service actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.seqera import (
    SeqeraAPIError,
    _extract_workflow_type,
    cancel_seqera_workflow,
    delete_seqera_workflow,
    delete_seqera_workflows,
)


def test_extract_workflow_type():
    """Test workflow type extraction heuristics."""
    assert _extract_workflow_type({"pipeline": "bindflow"}) == "BindCraft"
    assert _extract_workflow_type({"projectName": "Hello", "pipeline": ""}) == "Hello World"
    assert _extract_workflow_type({"projectName": "Custom", "pipeline": ""}) == "Custom"


@pytest.mark.asyncio
async def test_cancel_seqera_workflow_calls_client():
    """Test cancel delegates to client helper."""
    with patch("app.services.seqera.cancel_workflow_raw", new_callable=AsyncMock) as mock_cancel:
        await cancel_seqera_workflow("wf-1")
    mock_cancel.assert_called_once_with("wf-1", workspace_id=None)


@pytest.mark.asyncio
async def test_cancel_seqera_workflow_propagates_error():
    """Test cancel propagates client errors."""
    with patch(
        "app.services.seqera.cancel_workflow_raw",
        new_callable=AsyncMock,
        side_effect=SeqeraAPIError("fail"),
    ):
        with pytest.raises(SeqeraAPIError, match="fail"):
            await cancel_seqera_workflow("wf-1")


@pytest.mark.asyncio
async def test_delete_seqera_workflow_404_no_error(monkeypatch):
    """Test delete delegates to client helper."""
    with patch("app.services.seqera.delete_workflow_raw", new_callable=AsyncMock) as mock_delete:
        await delete_seqera_workflow("wf-404")
    mock_delete.assert_called_once_with("wf-404", workspace_id=None)


@pytest.mark.asyncio
async def test_delete_seqera_workflow_error(monkeypatch):
    """Test delete propagates client errors."""
    with patch(
        "app.services.seqera.delete_workflow_raw",
        new_callable=AsyncMock,
        side_effect=SeqeraAPIError("boom"),
    ):
        with pytest.raises(SeqeraAPIError, match="boom"):
            await delete_seqera_workflow("wf-500")


@pytest.mark.asyncio
async def test_delete_seqera_workflows_calls_client(monkeypatch):
    """Test bulk delete delegates to client."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WORK_SPACE", "test-workspace")

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=ok) as mock_post:
        await delete_seqera_workflows(["wf-1", "wf-2"])
    assert mock_post.call_count == 1
