"""Coverage tests for low-level Seqera client helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.seqera_client import (
    SeqeraClient,
    cancel_workflow_raw,
    delete_workflow_raw,
    delete_workflows_raw,
    describe_workflow_raw,
    get_workflow_logs_raw,
    list_workflows_raw,
)
from app.services.seqera_errors import SeqeraAPIError, SeqeraConfigurationError


@pytest.mark.asyncio
async def test_seqera_client_post_uses_default_headers(mock_settings):
    mock_settings.seqera.access_token = "token"
    ok = AsyncMock(spec=httpx.Response)

    client = SeqeraClient(settings=mock_settings)

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        response = await client.post(
            "/workflow/launch",
            payload={"launch": {"runName": "test"}},
        )

    assert response is ok
    mock_post.assert_awaited_once_with(
        "https://api.seqera.test/workflow/launch",
        headers={
            "Authorization": "Bearer token",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"launch": {"runName": "test"}},
    )


@pytest.mark.asyncio
async def test_seqera_client_post_headers_override_defaults(mock_settings):
    mock_settings.seqera.access_token = "token"
    ok = AsyncMock(spec=httpx.Response)

    client = SeqeraClient(settings=mock_settings)

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        await client.post(
            "/workflow/launch",
            headers={"Accept": "application/vnd.seqera+json"},
            payload={"launch": {}},
        )

    mock_post.assert_awaited_once_with(
        "https://api.seqera.test/workflow/launch",
        headers={
            "Authorization": "Bearer token",
            "Accept": "application/vnd.seqera+json",
            "Content-Type": "application/json",
        },
        json={"launch": {}},
    )


@pytest.mark.asyncio
async def test_list_workflows_raw_missing_config(mocker):
    mocker.patch(
        "app.services.seqera_client.get_settings",
        side_effect=SeqeraConfigurationError("Missing required setting"),
    )
    with pytest.raises(SeqeraConfigurationError, match="Missing required setting"):
        await list_workflows_raw()


@pytest.mark.asyncio
async def test_describe_and_list_success(mock_settings):
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "token"

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False
    ok.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient.get", return_value=ok):
        assert await list_workflows_raw(settings=mock_settings) == {"ok": True}
        assert await describe_workflow_raw("wf-1", settings=mock_settings) == {"ok": True}
        assert await get_workflow_logs_raw("wf-1", settings=mock_settings) == {"ok": True}


@pytest.mark.asyncio
async def test_list_workflows_raw_passes_max_and_search_params(mock_settings):
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "token"
    mock_settings.seqera.work_space = "ws-1"

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False
    ok.json.return_value = {"workflows": []}

    with patch("httpx.AsyncClient.get", return_value=ok) as mock_get:
        result = await list_workflows_raw(
            search_query="status:RUNNING", max_results=100, settings=mock_settings
        )

    assert result == {"workflows": []}
    mock_get.assert_awaited_once_with(
        "https://api.seqera.test/workflow",
        params={"workspaceId": "ws-1", "search": "status:RUNNING", "max": "100"},
        headers={"Authorization": "Bearer token", "Accept": "application/json"},
    )


@pytest.mark.asyncio
async def test_list_workflows_raw_uses_settings_workspace_when_not_given(mock_settings):
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "token"
    mock_settings.seqera.work_space = "ws-default"

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False
    ok.json.return_value = {"workflows": []}

    with patch("httpx.AsyncClient.get", return_value=ok) as mock_get:
        await list_workflows_raw(settings=mock_settings)

    mock_get.assert_awaited_once_with(
        "https://api.seqera.test/workflow",
        params={"workspaceId": "ws-default"},
        headers={"Authorization": "Bearer token", "Accept": "application/json"},
    )


@pytest.mark.asyncio
async def test_cancel_and_delete_paths(mock_settings):
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "token"

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        await cancel_workflow_raw("wf-1", settings=mock_settings)
        assert mock_post.call_count == 1

    not_found = AsyncMock(spec=httpx.Response)
    not_found.status_code = 404
    not_found.is_error = True
    not_found.text = "missing"

    with patch("httpx.AsyncClient.delete", return_value=not_found):
        await delete_workflow_raw("wf-1", settings=mock_settings)

    ok_post = AsyncMock(spec=httpx.Response)
    ok_post.is_error = False

    with patch("httpx.AsyncClient.post", return_value=ok_post):
        await delete_workflows_raw(["wf-1", "wf-2"], settings=mock_settings)


@pytest.mark.asyncio
async def test_cancel_and_delete_errors(mock_settings):
    mock_settings.seqera.api_url = "https://api.seqera.test"
    mock_settings.seqera.access_token = "token"

    err = AsyncMock(spec=httpx.Response)
    err.is_error = True
    err.status_code = 500
    err.text = "err"

    with patch("httpx.AsyncClient.post", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await cancel_workflow_raw("wf-1", settings=mock_settings)

    with patch("httpx.AsyncClient.delete", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await delete_workflow_raw("wf-1", settings=mock_settings)

    with patch("httpx.AsyncClient.post", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await delete_workflows_raw(["wf-1"], settings=mock_settings)
