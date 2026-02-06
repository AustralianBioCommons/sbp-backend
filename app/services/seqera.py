"""Seqera Platform API integration for workflow operations."""

from __future__ import annotations

import logging
import os

import httpx

# Re-export for backward compatibility with tests
from .seqera_models import WorkflowListItem

logger = logging.getLogger(__name__)


class SeqeraConfigurationError(RuntimeError):
    """Raised when required Seqera configuration is missing."""


class SeqeraAPIError(RuntimeError):
    """Raised when Seqera API calls fail."""


def _get_required_env(key: str) -> str:
    """Get required environment variable or raise error."""
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


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
