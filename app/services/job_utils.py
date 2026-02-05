"""Helpers for job ownership, score handling, and Seqera payload parsing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import RunMetric, Workflow, WorkflowRun
from .s3 import S3ConfigurationError, S3ServiceError, calculate_csv_column_max


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


def get_workflow_type_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, str]:
    """Return workflow type labels from the local DB workflows table."""
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, Workflow.name)
        .outerjoin(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {seqera_run_id: workflow_name for seqera_run_id, workflow_name in rows if workflow_name}


async def ensure_completed_run_score(db: Session, run: WorkflowRun, ui_status: str) -> float | None:
    if ui_status != "Completed":
        return None

    existing = db.execute(select(RunMetric).where(RunMetric.run_id == run.id)).scalar_one_or_none()
    if existing and existing.max_score is not None:
        return _round_score(existing.max_score)

    file_key = f"results/{run.seqera_run_id}/ranker/s1_final_design_stats.csv"
    try:
        max_score = await calculate_csv_column_max(file_key=file_key, column_name="Average_i_pTM")
    except (S3ConfigurationError, S3ServiceError, ValueError):
        return None

    bounded_score = max(0.0, min(1.0, float(max_score)))
    if existing:
        existing.max_score = bounded_score
    else:
        db.add(RunMetric(run_id=run.id, max_score=bounded_score))
    db.commit()
    return _round_score(bounded_score)
