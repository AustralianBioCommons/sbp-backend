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
async def test_seqera_client_post_uses_default_headers(monkeypatch):
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token")
    ok = AsyncMock(spec=httpx.Response)

    client = SeqeraClient()

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        response = await client.post(
            "https://api.seqera.test/workflow/launch",
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
async def test_seqera_client_post_headers_override_defaults(monkeypatch):
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token")
    ok = AsyncMock(spec=httpx.Response)

    client = SeqeraClient()

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        await client.post(
            "https://api.seqera.test/workflow/launch",
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
async def test_list_workflows_raw_missing_config(monkeypatch):
    monkeypatch.delenv("SEQERA_API_URL", raising=False)
    with pytest.raises(SeqeraConfigurationError):
        await list_workflows_raw()


@pytest.mark.asyncio
async def test_describe_and_list_success(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token")

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False
    ok.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient.get", return_value=ok):
        assert await list_workflows_raw() == {"ok": True}
        assert await describe_workflow_raw("wf-1") == {"ok": True}
        assert await get_workflow_logs_raw("wf-1") == {"ok": True}


@pytest.mark.asyncio
async def test_cancel_and_delete_paths(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token")

    ok = AsyncMock(spec=httpx.Response)
    ok.is_error = False

    with patch("httpx.AsyncClient.post", return_value=ok) as mock_post:
        await cancel_workflow_raw("wf-1")
        assert mock_post.call_count == 1

    not_found = AsyncMock(spec=httpx.Response)
    not_found.status_code = 404
    not_found.is_error = True
    not_found.text = "missing"

    with patch("httpx.AsyncClient.delete", return_value=not_found):
        await delete_workflow_raw("wf-1")

    ok_post = AsyncMock(spec=httpx.Response)
    ok_post.is_error = False

    with patch("httpx.AsyncClient.post", return_value=ok_post):
        await delete_workflows_raw(["wf-1", "wf-2"])


@pytest.mark.asyncio
async def test_cancel_and_delete_errors(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token")

    err = AsyncMock(spec=httpx.Response)
    err.is_error = True
    err.status_code = 500
    err.text = "err"

    with patch("httpx.AsyncClient.post", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await cancel_workflow_raw("wf-1")

    with patch("httpx.AsyncClient.delete", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await delete_workflow_raw("wf-1")

    with patch("httpx.AsyncClient.post", return_value=err):
        with pytest.raises(SeqeraAPIError):
            await delete_workflows_raw(["wf-1"])
