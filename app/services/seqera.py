"""Seqera Platform API integration for workflow operations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from .seqera_client import SeqeraClient, list_workflows_raw
from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError

logger = logging.getLogger(__name__)

# Pipeline statuses that still occupy a slot on Gadi (queued or actively running).
ACTIVE_PIPELINE_STATUSES = frozenset({"SUBMITTED", "RUNNING"})


class WorkflowExecutorError(RuntimeError):
    """Raised when workflow execution through Seqera fails."""


@dataclass
class WorkflowLaunchResult:
    """Result of a workflow launch."""

    workflow_id: str
    status: str
    message: str | None = None


def params_to_yaml_text(params: dict[str, Any]) -> str:
    """Serialize a params dict to a YAML string for Seqera paramsText."""
    if not params:
        return ""
    return str(yaml.dump(params, default_flow_style=False, sort_keys=False)).rstrip()


async def post_seqera_launch(
    payload: dict[str, Any],
    *,
    workflow_label: str,
) -> WorkflowLaunchResult:
    """Post a workflow launch payload to Seqera and return the launch result."""
    seqera_client = SeqeraClient()
    workspace_id = _get_required_env("WORK_SPACE")
    path = f"/workflow/launch?workspaceId={workspace_id}"
    response = await seqera_client.post(path, payload)

    if response.is_error:
        body = response.text
        logger.error(
            "Seqera API error %s %s: %s",
            response.status_code,
            response.reason_phrase,
            body,
        )
        raise WorkflowExecutorError(
            f"{workflow_label} workflow launch failed: {response.status_code} {body}"
        )

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    if not workflow_id:
        raise WorkflowExecutorError(
            f"{workflow_label} workflow launch succeeded but did not return a workflowId"
        )

    return WorkflowLaunchResult(
        workflow_id=workflow_id,
        status=data.get("status", "submitted"),
        message=data.get("message"),
    )


async def count_active_workflows(workspace_id: str | None = None) -> int:
    """Count Seqera workflow runs that are still queued or running on Gadi.

    Used to cap how many new workflows the scheduler submits at once, since each
    run holds a slice of Gadi's shared PBS job-slot limit for its whole lifetime.

    Queries each active status separately via the API's `search=status:<value>`
    filter and reads the server-computed `totalSize`, rather than fetching a page
    of workflows and counting client-side - `totalSize` reflects the full filtered
    count regardless of the page size, so this is exact even if there are more
    matching runs than fit on one page.
    """
    total = 0
    for status in ACTIVE_PIPELINE_STATUSES:
        data = await list_workflows_raw(
            workspace_id, search_query=f"status:{status}", max_results=1
        )
        if isinstance(data, dict):
            total += data.get("totalSize") or 0
    return total


def _get_required_env(key: str) -> str:
    """Get required environment variable or raise error."""
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


def _samplesheet_url(seqera_api_url: str, workspace_id: str, dataset_id: str) -> str:
    """Build the Seqera samplesheet URL for a dataset."""
    return (
        f"{seqera_api_url}/workspaces/{workspace_id}"
        f"/datasets/{dataset_id}/v/1/n/samplesheet.csv"
    )


def _extract_workflow_type(workflow_data: dict) -> str | None:
    """
    Extract workflow type from workflow data.

    This could be based on:
    - Pipeline name/repository
    - Project name
    - Custom metadata
    """
    # Get project name or pipeline
    project_name = workflow_data.get("projectName", "")
    pipeline = workflow_data.get("pipeline", "")

    # Combine for checking
    full_name = f"{project_name} {pipeline}".lower()

    # Map common pipeline names to workflow types
    if "bindflow" in full_name or "bindcraft" in full_name:
        return "BindCraft"
    elif "denovo" in full_name or "de-novo" in full_name:
        return "De novo design"
    elif "proteinfold" in full_name:
        return "ProteinFold"
    elif "aus-seqera-test" in full_name:
        return "Test Pipeline"
    elif "hello" in full_name:
        return "Hello World"

    # Return the project name if available, otherwise pipeline
    return project_name or pipeline or None


async def describe_workflow(workflow_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    """
    Get detailed information about a specific workflow run.

    Args:
        workflow_id: Seqera workflow run ID
        workspace_id: Seqera workspace ID (uses env var if not provided)

    Returns:
        Workflow details dictionary
    """
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")

    if not workspace_id:
        workspace_id = _get_required_env("WORK_SPACE")

    url = f"{seqera_api_url}/workflow/{workflow_id}"
    params = {"workspaceId": workspace_id}

    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Accept": "application/json",
    }

    logger.info(
        "Describing workflow from Seqera API",
        extra={"url": url, "workflow_id": workflow_id, "workspace_id": workspace_id},
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=headers, params=params)

    if response.is_error:
        body = response.text
        logger.error(f"Seqera API error: {response.status_code}. {response.reason_phrase}.\n{body}")
        raise SeqeraAPIError(
            f"Failed to describe workflow: {response.status_code} {body}",
            status_code=response.status_code,
        )

    result: dict[str, Any] = response.json()
    return result
