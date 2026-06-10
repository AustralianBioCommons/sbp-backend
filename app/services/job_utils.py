"""Helpers for job ownership, score handling, and Seqera payload parsing."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import RunMetric, Workflow, WorkflowRun
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


def get_owned_run_ids(db: Session, user_id: UUID) -> set[str]:
    rows = db.execute(
        select(WorkflowRun.seqera_run_id).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.seqera_run_id.is_not(None),
        )
    ).all()
    return {row[0] for row in rows}


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


def get_score_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, float]:
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, RunMetric.max_score)
        .outerjoin(RunMetric, RunMetric.run_id == WorkflowRun.id)
        .where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {
        str(seqera_run_id): rounded
        for seqera_run_id, score in rows
        if seqera_run_id and (rounded := _round_score(score)) is not None
    }


def format_workflow_name(name: str) -> str:
    """Format a workflow slug for display: 'de-novo-design' → 'De Novo Design'."""
    return " ".join(word.capitalize() for word in name.replace("-", " ").split())


def format_tool_name(name: str) -> str:
    """Uppercase the first character of a tool id: 'colabfold' → 'Colabfold'."""
    return name[0].upper() + name[1:] if name else name


def get_workflow_type_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, str]:
    """Return workflow type labels from the local DB workflows table."""
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, Workflow.name)
        .outerjoin(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {
        seqera_run_id: format_workflow_name(workflow_name)
        for seqera_run_id, workflow_name in rows
        if workflow_name
    }


def get_tool_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, str]:
    """Return tool names keyed by seqera_run_id.

    Reads from workflow_runs.tool; falls back to submitted_form_data for older rows.
    Returns 'Unknown' when no value is found.
    """
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, WorkflowRun.tool, WorkflowRun.submitted_form_data).where(
            WorkflowRun.owner_user_id == user_id
        )
    ).all()
    result: dict[str, str] = {}
    for seqera_run_id, tool_col, form_data in rows:
        if not seqera_run_id:
            continue
        tool: str | None = tool_col or None
        if not tool and isinstance(form_data, dict):
            for key in ("tool", "mode"):
                raw = form_data.get(key)
                if raw:
                    candidate = str(raw).strip()
                    if candidate:
                        tool = candidate
                        break
        result[seqera_run_id] = format_tool_name(tool) if tool else "Unknown"
    return result


def _get_sample_id_for_score(run: WorkflowRun) -> str | None:
    return get_sample_id_for_result(run)


async def ensure_completed_run_score(db: Session, run: WorkflowRun, ui_status: str) -> float | None:
    if ui_status != "Completed":
        return None

    existing = db.execute(select(RunMetric).where(RunMetric.run_id == run.id)).scalar_one_or_none()
    if existing and existing.max_score is not None:
        return _round_score(existing.max_score)

    spec = get_output_spec(run)
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
