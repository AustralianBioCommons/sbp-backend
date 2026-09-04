"""Helpers for job ownership, score handling, and Seqera payload parsing."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, false, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from ..config import Settings
from ..db.models.core import RunMetric, WorkflowRun
from ..db.models.job_queue import JobStatus, QueuedJob
from ..schemas.workflows.shared import TERMINAL_SEQERA_STATUSES, PipelineStatus, UIStatus
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
    queued_status: JobStatus | None

    @property
    def is_pending(self) -> bool:
        return self.queued_status == "pending"


def get_user_job_list_rows_select(user_id: UUID) -> Select[tuple[WorkflowRun, JobStatus | None]]:
    queued_job_status = cast(
        ColumnElement[JobStatus | None],
        (
            select(QueuedJob.status)
            .where(
                QueuedJob.workflow_run_id == WorkflowRun.id,
            )
            .order_by(QueuedJob.queued_at.desc())
            .limit(1)
            .scalar_subquery()
        ),
    )
    return (
        select(WorkflowRun, queued_job_status)
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
            queued_status=queued_status,
        )
        for run, queued_status in rows
    ]


def _build_db_status_filter(
    allowed_statuses: set[str],
    queued_status_col: ColumnElement[JobStatus | None],
    score_col: ColumnElement[float | None],
) -> ColumnElement[bool] | None:
    """Express an allowed-statuses filter as SQL, for statuses derivable from stored
    columns alone. Callers must exclude LIVE_ONLY_UI_STATUSES first - those (In queue,
    In progress) only exist between "submitted to Seqera" and "terminal", which isn't
    persisted anywhere (see job_sync.py's non-terminal branch), so they can't be
    expressed here.
    """
    if not allowed_statuses:
        return None

    seqera_upper = func.upper(WorkflowRun.seqera_final_status)
    not_locally_queued = or_(
        queued_status_col.is_(None), queued_status_col.not_in(["pending", "staging", "failed"])
    )
    not_terminal = or_(
        WorkflowRun.seqera_final_status.is_(None),
        seqera_upper.not_in(tuple(TERMINAL_SEQERA_STATUSES)),
    )

    # Mirrors the per-row branching in routes/workflow/jobs.py's list_jobs.
    clause_by_status: dict[str, ColumnElement[bool]] = {
        UIStatus.COMPLETED.value: or_(
            seqera_upper == PipelineStatus.SUCCEEDED.value,
            # Seqera unreachable but we already have a cached score from a prior sync.
            and_(not_locally_queued, not_terminal, score_col.is_not(None)),
        ),
        UIStatus.FAILED.value: or_(
            queued_status_col == "failed",
            seqera_upper.in_([PipelineStatus.FAILED.value, PipelineStatus.UNKNOWN.value]),
        ),
        UIStatus.STOPPED.value: seqera_upper == PipelineStatus.CANCELLED.value,
        "Pending": queued_status_col == "pending",
        "Staging": queued_status_col == "staging",
    }
    clauses = [
        clause_by_status[status] for status in allowed_statuses if status in clause_by_status
    ]
    if not clauses:
        return false()
    return or_(*clauses)


def get_user_job_list_page_select(
    user_id: UUID,
    allowed_statuses: set[str],
    sort_by: str,
    sort_order: str,
) -> Select[tuple[WorkflowRun, JobStatus | None]]:
    queued_job_status = cast(
        ColumnElement[JobStatus | None],
        (
            select(QueuedJob.status)
            .where(QueuedJob.workflow_run_id == WorkflowRun.id)
            .order_by(QueuedJob.queued_at.desc())
            .limit(1)
            .scalar_subquery()
        ),
    )
    score_metric = aliased(RunMetric)

    stmt = (
        select(WorkflowRun, queued_job_status)
        .outerjoin(score_metric, score_metric.run_id == WorkflowRun.id)
        .options(joinedload(WorkflowRun.workflow), joinedload(WorkflowRun.metrics))
        .where(WorkflowRun.owner_user_id == user_id)
    )

    score_col = cast(ColumnElement[float | None], score_metric.max_score)
    status_filter = _build_db_status_filter(allowed_statuses, queued_job_status, score_col)
    if status_filter is not None:
        stmt = stmt.where(status_filter)

    if sort_by == "score":
        primary: ColumnElement[Any] = score_col.asc() if sort_order == "asc" else score_col.desc()
        stmt = stmt.order_by(primary.nulls_last(), WorkflowRun.submission_timestamp.desc())
    else:
        primary = (
            WorkflowRun.submission_timestamp.asc()
            if sort_order == "asc"
            else WorkflowRun.submission_timestamp.desc()
        )
        stmt = stmt.order_by(primary.nulls_last())

    return stmt


def get_user_job_list_page(
    db: Session,
    user_id: UUID,
    *,
    allowed_statuses: set[str],
    sort_by: str,
    sort_order: str,
    limit: int,
    offset: int,
) -> tuple[list[UserJobListRow], int]:
    """DB-level sort/filter/paginate. Only valid when `allowed_statuses` excludes
    LIVE_ONLY_UI_STATUSES - see `_build_db_status_filter`.
    """
    base_stmt = get_user_job_list_page_select(user_id, allowed_statuses, sort_by, sort_order)

    total = db.scalar(select(func.count()).select_from(base_stmt.order_by(None).subquery())) or 0

    rows = db.execute(base_stmt.limit(limit).offset(offset)).all()
    page = [
        UserJobListRow(
            run=run,
            run_id=str(run.id),
            seqera_run_id=run.seqera_run_id,
            workflow_type=_get_workflow_type(run),
            tool=_get_tool(run),
            score=_round_score(run.metrics.max_score) if run.metrics else None,
            final_design_count=_get_final_design_count(run),
            queued_status=queued_status,
        )
        for run, queued_status in rows
    ]
    return page, total


def get_owned_run_by_id(db: Session, user_id: UUID, run_id: str) -> WorkflowRun | None:
    try:
        workflow_run_id = UUID(str(run_id))
    except ValueError:
        return None

    return db.execute(
        select(WorkflowRun).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.id == workflow_run_id,
        )
    ).scalar_one_or_none()


def get_owned_run_by_seqera_id(
    db: Session, user_id: UUID, seqera_run_id: str
) -> WorkflowRun | None:
    return db.execute(
        select(WorkflowRun).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.seqera_run_id == seqera_run_id,
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


async def ensure_completed_run_score(
    db: Session, run: WorkflowRun, ui_status: str, settings: Settings | None = None
) -> float | None:
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

    if settings is None:
        await sync_workflow_outputs(db, run=run, spec=spec, suppress_s3_errors=True)
    else:
        await sync_workflow_outputs(
            db, run=run, spec=spec, suppress_s3_errors=True, settings=settings
        )

    if settings is None:
        max_score = await spec.get_max_score(db, run)
    else:
        max_score = await spec.get_max_score(db, run, settings=settings)
    if max_score is None:
        return None

    bounded_score = max(0.0, min(1.0, float(max_score)))
    if existing:
        existing.max_score = bounded_score
    else:
        db.add(RunMetric(run_id=run.id, max_score=bounded_score))
    db.commit()
    return _round_score(bounded_score)


async def sync_service_usage(
    db: Session, run: WorkflowRun, ui_status: str, settings: Settings | None = None
) -> float | None:
    if ui_status != "Completed":
        return None
    if run.service_usage is not None:
        return run.service_usage

    try:
        spec = get_output_spec(run)
    except ValueError as exc:
        logger.warning(f"Can't get service usage for run {run.id} - can't get output spec: {exc}")
        return None

    if settings is None:
        await sync_workflow_outputs(db, run=run, spec=spec, suppress_s3_errors=True)
    else:
        await sync_workflow_outputs(
            db, run=run, spec=spec, suppress_s3_errors=True, settings=settings
        )

    if settings is None:
        usage = await spec.get_service_units(db, run)
    else:
        usage = await spec.get_service_units(db, run, settings=settings)
    if usage is None:
        return None
    else:
        run.service_usage = usage
        db.add(run)
        db.commit()
        return usage
