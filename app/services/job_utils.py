"""Helpers for job ownership, score handling, and Seqera payload parsing."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from ..db.models.core import RunMetric, WorkflowRun
from ..db.models.job_queue import QueuedJob
from .results_utils import (
    get_output_spec,
    get_sample_id_for_result,
    sync_workflow_outputs,
)

logger = logging.getLogger(__name__)


def coerce_workflow_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    workflow = payload.get("workflow")
    if isinstance(workflow, Mapping):
        return dict(workflow)
    return dict(payload)


def extract_pipeline_status(payload: Mapping[str, Any]) -> str:
    workflow = coerce_workflow_payload(payload)
    return str(workflow.get("status") or "UNKNOWN")


def parse_submit_datetime(payload: Mapping[str, Any]) -> datetime | None:
    workflow = coerce_workflow_payload(payload)
    submit_str = workflow.get("submit") or workflow.get("dateCreated")
    if not submit_str:
        return None
    try:
        return datetime.fromisoformat(str(submit_str).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class UserJobListRow:
    """
    Entries used for list_jobs endpoint
    """
    run: WorkflowRun
    run_id: str
    seqera_run_id: str | None
    workflow_type: str
    tool: str
    score: float | None
    final_design_count: int | None
    is_pending: bool


def get_user_job_list_rows_select(user_id: UUID) -> Select[tuple[WorkflowRun, bool]]:
    pending_queued_job_exists = (
        select(QueuedJob.id)
        .where(
            QueuedJob.workflow_run_id == WorkflowRun.id,
            QueuedJob.status == "pending",
        )
        .exists()
    )
    return (
        select(WorkflowRun, pending_queued_job_exists)
        .options(
            joinedload(WorkflowRun.workflow),
            joinedload(WorkflowRun.metrics),
        )
        .where(WorkflowRun.owner_user_id == user_id)
    )


def get_user_job_list_rows(db: Session, user_id: UUID) -> list[UserJobListRow]:
    rows = db.execute(get_user_job_list_rows_select(user_id)).all()
    return [
        UserJobListRow(
            run=run,
            run_id=str(run.id),
            seqera_run_id=run.seqera_run_id,
            workflow_type=_get_workflow_type(run),
            tool=_get_tool(run),
            score=_round_score(run.metrics.max_score) if run.metrics else None,
            final_design_count=_get_final_design_count(run),
            is_pending=is_pending,
        )
        for run, is_pending in rows
    ]


def get_owned_run(db: Session, user_id: UUID, run_id: str) -> WorkflowRun | None:
    return db.execute(
        select(WorkflowRun).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.seqera_run_id == run_id,
        )
    ).scalar_one_or_none()


def _round_score(value: float | Decimal | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def format_workflow_name(name: str) -> str:
    """Format a workflow slug for display: 'de-novo-design' → 'De Novo Design'."""
    return " ".join(word.capitalize() for word in name.replace("-", " ").split())


def format_tool_name(name: str) -> str:
    """Uppercase the first character of a tool id: 'colabfold' → 'Colabfold'."""
    return name[0].upper() + name[1:] if name else name


def _get_workflow_type(run: WorkflowRun) -> str:
    return format_workflow_name(run.workflow.name) if run.workflow else "Unknown"


def _get_tool(run: WorkflowRun) -> str:
    tool: str | None = run.tool or None
    if not tool and isinstance(run.submitted_form_data, dict):
        for key in ("tool", "mode"):
            raw = run.submitted_form_data.get(key)
            if raw:
                candidate = str(raw).strip()
                if candidate:
                    tool = candidate
                    break
    return format_tool_name(tool) if tool else "Unknown"


def _get_final_design_count(run: WorkflowRun) -> int | None:
    if not run.metrics:
        return None
    value = run.metrics.final_design_count
    return value if isinstance(value, int) else None


def _get_sample_id_for_score(run: WorkflowRun) -> str | None:
    return get_sample_id_for_result(run)


async def ensure_completed_run_score(db: Session, run: WorkflowRun, ui_status: str) -> float | None:
    if ui_status != "Completed":
        return None

    existing = db.execute(select(RunMetric).where(RunMetric.run_id == run.id)).scalar_one_or_none()
    if existing and existing.max_score is not None:
        return _round_score(existing.max_score)

    # Score computation is best-effort: a run with an unknown workflow/tool (e.g. a
    # missing workflow relationship) has no output spec. Don't let that take down the
    # whole job list — log and skip the score for this run.
    try:
        spec = get_output_spec(run)
    except ValueError as exc:
        logger.warning("Skipping score for run %s: %s", run.id, exc)
        return None

    await sync_workflow_outputs(db, run=run, spec=spec, suppress_s3_errors=True)

    max_score = await spec.get_max_score(db, run)
    if max_score is None:
        return None

    bounded_score = max(0.0, min(1.0, float(max_score)))
    if existing:
        existing.max_score = bounded_score
    else:
        db.add(RunMetric(run_id=run.id, max_score=bounded_score))
    db.commit()
    return _round_score(bounded_score)
