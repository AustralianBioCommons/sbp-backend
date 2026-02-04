"""Seqera Platform API integration for workflow operations."""

from __future__ import annotations

from .seqera_client import (
    cancel_workflow_raw,
    delete_workflow_raw,
    describe_workflow_raw,
    list_workflows_raw,
)
from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError
from .seqera_models import WorkflowListItem
from .seqera_parsers import parse_workflow_list_payload

__all__ = [
    "SeqeraAPIError",
    "SeqeraConfigurationError",
    "WorkflowListItem",
    "list_seqera_workflows",
    "describe_workflow",
    "cancel_seqera_workflow",
    "delete_seqera_workflow",
]


async def list_seqera_workflows(
    workspace_id: str | None = None,
    search_query: str | None = None,
    status_filter: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkflowListItem], int]:
    """List workflow runs from Seqera Platform."""
    _ = (limit, offset)  # API keeps these for backward compatibility.
    data = await list_workflows_raw(workspace_id=workspace_id, search_query=search_query)
    return parse_workflow_list_payload(
        data,
        status_filter=status_filter,
        search_query=search_query,
    )


async def describe_workflow(workflow_id: str, workspace_id: str | None = None) -> dict:
    """Get detailed information about a specific workflow run."""
    return await describe_workflow_raw(workflow_id=workflow_id, workspace_id=workspace_id)


async def cancel_seqera_workflow(workflow_id: str, workspace_id: str | None = None) -> None:
    """Cancel a Seqera workflow run."""
    await cancel_workflow_raw(workflow_id=workflow_id, workspace_id=workspace_id)


async def delete_seqera_workflow(workflow_id: str, workspace_id: str | None = None) -> None:
    """Delete a Seqera workflow run."""
    await delete_workflow_raw(workflow_id=workflow_id, workspace_id=workspace_id)
