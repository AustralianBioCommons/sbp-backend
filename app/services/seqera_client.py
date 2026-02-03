"""Low-level HTTP calls to Seqera API."""

from __future__ import annotations

import os

import httpx

from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError


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
) -> dict | list:
    api_url, token, params = _get_api_context(workspace_id)
    if search_query:
        params["search"] = search_query

    url = f"{api_url}/workflow"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=_headers(token), params=params)

    if response.is_error:
        raise SeqeraAPIError(f"Failed to list workflows: {response.status_code} {response.text}")
    return response.json()


async def describe_workflow_raw(workflow_id: str, workspace_id: str | None = None) -> dict:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=_headers(token), params=params)

    if response.is_error:
        raise SeqeraAPIError(f"Failed to describe workflow: {response.status_code} {response.text}")
    return response.json()


async def cancel_workflow_raw(workflow_id: str, workspace_id: str | None = None) -> None:
    api_url, token, params = _get_api_context(workspace_id)
    candidate_paths = [
        f"{api_url}/workflow/{workflow_id}/cancel",
        f"{api_url}/workflow/{workflow_id}/kill",
    ]
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        last_error = None
        for url in candidate_paths:
            response = await client.post(url, headers=_headers(token), params=params)
            if not response.is_error:
                return
            last_error = f"{response.status_code} {response.text}"
    raise SeqeraAPIError(f"Failed to cancel workflow {workflow_id}: {last_error}")


async def delete_workflow_raw(workflow_id: str, workspace_id: str | None = None) -> None:
    api_url, token, params = _get_api_context(workspace_id)
    url = f"{api_url}/workflow/{workflow_id}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.delete(url, headers=_headers(token), params=params)

    if response.status_code == 404:
        return
    if response.is_error:
        raise SeqeraAPIError(f"Failed to delete workflow {workflow_id}: {response.status_code} {response.text}")
