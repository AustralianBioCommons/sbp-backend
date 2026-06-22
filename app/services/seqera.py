"""Seqera Platform API integration for workflow operations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from .seqera_client import SeqeraClient
from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError

logger = logging.getLogger(__name__)


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
    url: str,
    payload: dict[str, Any],
    *,
    workflow_label: str,
) -> WorkflowLaunchResult:
    """Post a workflow launch payload to Seqera and return the launch result."""
    seqera_client = SeqeraClient()
    response = await seqera_client.post(url, payload)

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
        logger.error(
            "Seqera API error",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise SeqeraAPIError(
            f"Failed to describe workflow: {response.status_code} {body}",
            status_code=response.status_code,
        )

    result: dict[str, Any] = response.json()
    return result
