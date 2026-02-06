"""Shared Seqera service models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
