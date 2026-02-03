"""Parsing helpers for Seqera workflow payloads."""

from __future__ import annotations

from datetime import datetime

from ..schemas.workflows import map_pipeline_status_to_ui
from .seqera_models import WorkflowListItem


def extract_workflow_type(workflow_data: dict) -> str | None:
    """Extract workflow type from workflow data."""
    pipeline = workflow_data.get("projectName") or workflow_data.get("pipeline", "")

    if "bindcraft" in pipeline.lower():
        return "BindCraft"
    if "denovo" in pipeline.lower() or "de-novo" in pipeline.lower():
        return "De novo design"
    return pipeline or None


def parse_workflow_list_payload(
    data: dict | list,
    status_filter: list[str] | None = None,
    search_query: str | None = None,
) -> tuple[list[WorkflowListItem], int]:
    """Normalize Seqera list responses into workflow items."""
    if isinstance(data, dict):
        workflows_data = data.get("workflows") or data.get("items") or []
        total_count = data.get("totalSize") or data.get("total") or len(workflows_data)
    elif isinstance(data, list):
        workflows_data = data
        total_count = len(workflows_data)
    else:
        workflows_data = []
        total_count = 0

    items: list[WorkflowListItem] = []
    for item in workflows_data:
        wf = item.get("workflow", item) if isinstance(item, dict) else {}
        pipeline_status = wf.get("status", "UNKNOWN")
        ui_status = map_pipeline_status_to_ui(pipeline_status)

        if status_filter and ui_status not in status_filter:
            continue

        submitted_at = None
        if submit_str := wf.get("submit") or wf.get("dateCreated"):
            try:
                submitted_at = datetime.fromisoformat(submit_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                submitted_at = None

        items.append(
            WorkflowListItem(
                workflow_id=wf.get("id", ""),
                run_name=wf.get("runName"),
                workflow_type=extract_workflow_type(wf),
                pipeline_status=pipeline_status,
                ui_status=ui_status,
                submitted_at=submitted_at,
                score=None,
            )
        )

    if search_query:
        needle = search_query.lower()
        items = [
            wf
            for wf in items
            if needle in (wf.run_name or "").lower() or needle in (wf.workflow_type or "").lower()
        ]

    return items, total_count
