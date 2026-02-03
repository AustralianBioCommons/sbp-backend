"""Seqera Platform API integration for workflow operations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..schemas.workflows import map_pipeline_status_to_ui

logger = logging.getLogger(__name__)


class SeqeraConfigurationError(RuntimeError):
    """Raised when required Seqera configuration is missing."""


class SeqeraAPIError(RuntimeError):
    """Raised when Seqera API calls fail."""


@dataclass
class WorkflowListItem:
    """Individual workflow run item from Seqera API."""
    
    workflow_id: str
    run_name: str | None
    workflow_type: str | None
    pipeline_status: str
    ui_status: str
    submitted_at: datetime | None
    score: float | None


def _masked_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask sensitive headers before logging."""
    masked = dict(headers)
    if "Authorization" in masked:
        masked["Authorization"] = "Bearer ***"
    return masked


def _get_required_env(key: str) -> str:
    """Get required environment variable or raise error."""
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


async def list_seqera_workflows(
    workspace_id: str | None = None,
    search_query: str | None = None,
    status_filter: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkflowListItem], int]:
    """
    List workflow runs from Seqera Platform.
    
    Args:
        workspace_id: Seqera workspace ID (uses env var if not provided)
        search_query: Search term for job name or workflow type
        status_filter: List of UI status values to filter by (e.g., ["Completed", "Failed"])
        limit: Maximum number of results to return
        offset: Number of results to skip
        
    Returns:
        Tuple of (list of workflow items, total count)
    """
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    
    if not workspace_id:
        workspace_id = os.getenv("WORK_SPACE")

    # Build query parameters. Keep workspace optional to match plain GET /workflow usage.
    params: dict[str, str | int] = {}
    if workspace_id:
        params["workspaceId"] = workspace_id
    
    # Add search query if provided
    if search_query:
        params["search"] = search_query
    
    url = f"{seqera_api_url}/workflow"
    
    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Accept": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=headers, params=params)
    
    
    if response.is_error:
        body = response.text
        logger.error(
            "Seqera API error",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise SeqeraAPIError(
            f"Failed to list workflows: {response.status_code} {body}"
        )
    
    data = response.json()
    
    logger.info(
        "Parsed workflow data from Seqera",
        extra={
            "total_workflows": len(data.get("workflows", [])),
            "total_size": data.get("totalSize", 0),
        },
    )
    
    # Support both Seqera payload shapes:
    # 1) {"workflows": [...], "totalSize": N}
    # 2) [{"workflow": {...}}, ...]
    if isinstance(data, dict):
        workflows_data = data.get("workflows") or data.get("items") or []
        total_count = data.get("totalSize") or data.get("total") or len(workflows_data)
    elif isinstance(data, list):
        workflows_data = data
        total_count = len(workflows_data)
    else:
        workflows_data = []
        total_count = 0
    
    workflow_items: list[WorkflowListItem] = []
    
    for item in workflows_data:
        # Some Seqera responses wrap run data in {"workflow": {...}}
        wf = item.get("workflow", item) if isinstance(item, dict) else {}

        pipeline_status = wf.get("status", "UNKNOWN")
        ui_status = map_pipeline_status_to_ui(pipeline_status)
        
        # Apply status filter if provided
        if status_filter and ui_status not in status_filter:
            continue
        
        # Parse submitted date
        submitted_at = None
        if submit_str := wf.get("submit") or wf.get("dateCreated"):
            try:
                submitted_at = datetime.fromisoformat(submit_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse submit time: {submit_str}")
        
        workflow_items.append(
            WorkflowListItem(
                workflow_id=wf.get("id", ""),
                run_name=wf.get("runName"),
                workflow_type=_extract_workflow_type(wf),
                pipeline_status=pipeline_status,
                ui_status=ui_status,
                submitted_at=submitted_at,
                score=None
            )
        )
    
    logger.info(
        f"Returning {len(workflow_items)} workflow items after filtering",
        extra={"requested_status_filter": status_filter},
    )
    
    return workflow_items, total_count


def _extract_workflow_type(workflow_data: dict) -> str | None:
    """
    Extract workflow type from workflow data.
    
    This could be based on:
    - Pipeline name/repository
    - Project name
    - Custom metadata
    """
    pipeline = workflow_data.get("projectName") or workflow_data.get("pipeline", "")
    
    # Map common pipeline names to workflow types
    if "bindcraft" in pipeline.lower():
        return "BindCraft"
    elif "denovo" in pipeline.lower() or "de-novo" in pipeline.lower():
        return "De novo design"
    
    return pipeline or None


async def describe_workflow(workflow_id: str, workspace_id: str | None = None) -> dict:
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
        workspace_id = os.getenv("WORK_SPACE")
    
    url = f"{seqera_api_url}/workflow/{workflow_id}"
    params: dict[str, str] = {}
    if workspace_id:
        params["workspaceId"] = workspace_id
    
    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Accept": "application/json",
    }
    
    logger.info(
        "Describing workflow from Seqera API: url=%s workflow_id=%s workspace_id=%s params=%s headers=%s",
        url,
        workflow_id,
        workspace_id,
        params,
        _masked_headers(headers),
    )
    
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.get(url, headers=headers, params=params)

    logger.info(
        "Seqera describe API response: method=GET path=/workflow/%s status=%s raw_body=%s",
        workflow_id,
        response.status_code,
        response.text if response.text else None,
    )
    
    if response.is_error:
        body = response.text
        logger.error(
            "Seqera API error",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise SeqeraAPIError(
            f"Failed to describe workflow: {response.status_code} {body}"
        )
    
    return response.json()
