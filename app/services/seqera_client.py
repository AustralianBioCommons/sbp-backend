"""Low-level HTTP calls to Seqera API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, cast

import httpx

from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError


class SeqeraClient:
    """Async HTTP client wrapper for Seqera API calls."""
    api_url: str

    def __init__(
        self,
        timeout: httpx.Timeout | float = 60,
    ) -> None:
        seqera_token = os.getenv("SEQERA_ACCESS_TOKEN")
        self.default_headers = {
            "Authorization": f"Bearer {seqera_token}",
            "Accept": "application/json",
        }
        self.timeout = timeout
        self.api_url = _get_required_env("SEQERA_API_URL").rstrip("/")

    def get_url(self, path: str) -> str:
        return f"{self.api_url}/{path.lstrip('/')}"

    async def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {
            **self.default_headers,
            "Content-Type": "application/json",
            **dict(headers or {}),
        }
        url = self.get_url(path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(url, headers=request_headers, json=dict(payload))

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {**self.default_headers, **dict(headers or {})}
        url = self.get_url(path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.get(url, params=params, headers=request_headers)


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


def _get_api_context(workspace_id: str | None = None) -> tuple[str, str, dict[str, str]]:
    api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    token = _get_required_env("SEQERA_ACCESS_TOKEN")
    resolved_workspace = workspace_id or os.getenv("WORK_SPACE")
    params: dict[str, str] = {}
    if resolved_workspace:
        params["workspaceId"] = resolved_workspace
    return api_url, token, params


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


async def list_workflows_raw(
    workspace_id: str | None = None,
    search_query: str | None = None,
) -> dict[str, Any] | list[Any]:
    api_url, token, params = _get_api_context(workspace_id)
    if search_query:
        params["search"] = search_query

    url = f"{api_url}/workflow"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=_headers(token), params=params)

    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to list workflows: {response.status_code} {response.text}",
            status_code=response.status_code,
        )
    return cast(dict[str, Any] | list[Any], response.json())


async def describe_workflow_raw(
    workflow_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=_headers(token), params=params)

    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to describe workflow: {response.status_code} {response.text}",
            status_code=response.status_code,
        )
    return cast(dict[str, Any], response.json())


async def get_workflow_logs_raw(
    workflow_id: str,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}/log"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=_headers(token), params=params)

    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to retrieve workflow logs: {response.status_code} {response.text}",
            status_code=response.status_code,
        )
    return cast(dict[str, Any], response.json())


async def cancel_workflow_raw(workflow_id: str, workspace_id: str | None = None) -> None:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}/cancel"
    payload: dict[str, Any] = {}
    headers = _headers(token)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.post(url, headers=headers, params=params, json=payload)

    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to cancel workflow {workflow_id}: {response.status_code} {response.text}",
            status_code=response.status_code,
        )


async def delete_workflow_raw(workflow_id: str, workspace_id: str | None = None) -> None:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.delete(url, headers=_headers(token), params=params)

    if response.status_code == 404:
        return
    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to delete workflow {workflow_id}: {response.status_code} {response.text}",
            status_code=response.status_code,
        )


async def delete_workflows_raw(workflow_ids: list[str], workspace_id: str | None = None) -> None:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/delete"
    payload = {"workflowIds": workflow_ids}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.post(url, headers=_headers(token), params=params, json=payload)

    if response.is_error:
        raise SeqeraAPIError(
            f"Failed to delete workflows {workflow_ids}: {response.status_code} {response.text}",
            status_code=response.status_code,
        )
