"""Workflow status polling and result sync service."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ..config import Settings
from ..db.models.core import WorkflowRun
from ..schemas.workflows.shared import (
    TERMINAL_SEQERA_STATUSES,
    PipelineStatus,
    UIStatus,
    map_pipeline_status_to_ui,
)
from .job_utils import ensure_completed_run_score, extract_pipeline_status, sync_service_usage
from .results_utils import get_output_spec, sync_workflow_outputs
from .seqera import describe_workflow
from .seqera_errors import SeqeraAPIError, SeqeraConfigurationError

logger = logging.getLogger(__name__)

DescribeWorkflow = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class WorkflowRunSyncResult:
    """Outcome for syncing one workflow run."""

    run_id: UUID | str
    seqera_run_id: str | None
    seqera_status: str | None = None
    ui_status: str | None = None
    terminal: bool = False
    sync_completed: bool = False
    outputs_synced: int = 0
    skipped: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorkflowRunSyncBatchResult:
    """Outcome for one batch of workflow run sync work."""

    checked: int
    results: list[WorkflowRunSyncResult]

    @property
    def completed(self) -> int:
        return sum(1 for result in self.results if result.sync_completed)

    @property
    def errored(self) -> int:
        return sum(1 for result in self.results if result.error is not None)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.skipped)


def get_runs_requiring_sync(db: Session, *, limit: int = 100) -> list[WorkflowRun]:
    """
    Return submitted workflow runs that still need syncing:
    - Started on seqera (has run ID assigned)
    - Final status not recorded or sync not marked as complete
    """
    stmt = (
        select(WorkflowRun)
        .options(joinedload(WorkflowRun.workflow), joinedload(WorkflowRun.metrics))
        .where(
            WorkflowRun.seqera_run_id.is_not(None),
            or_(
                WorkflowRun.seqera_final_status.is_(None),
                WorkflowRun.sync_completed_at.is_(None),
            ),
        )
        .order_by(WorkflowRun.submission_timestamp.asc(), WorkflowRun.id)
        .limit(limit)
    )
    return list(db.scalars(stmt).unique())


async def sync_workflow_runs(
    db: Session,
    *,
    limit: int = 100,
    suppress_s3_errors: bool = False,
    describe_func: DescribeWorkflow = describe_workflow,
    settings: Settings | None = None,
) -> WorkflowRunSyncBatchResult:
    """Poll Seqera and sync results for a batch of workflow runs."""
    runs = get_runs_requiring_sync(db, limit=limit)
    results: list[WorkflowRunSyncResult] = []

    for run in runs:
        try:
            result = await sync_workflow_run(
                db,
                run,
                suppress_s3_errors=suppress_s3_errors,
                describe_func=describe_func,
                settings=settings,
            )
        except (SeqeraAPIError, SeqeraConfigurationError) as exc:
            db.rollback()
            logger.warning(
                "Failed to sync workflow run %s from Seqera: %s",
                run.id,
                exc,
            )
            result = _error_result(run, exc)
        except Exception as exc:
            db.rollback()
            logger.exception("Unexpected error syncing workflow run %s", run.id)
            result = _error_result(run, exc)
        results.append(result)

    return WorkflowRunSyncBatchResult(checked=len(runs), results=results)


async def sync_workflow_run(
    db: Session,
    run: WorkflowRun,
    *,
    force: bool = False,
    suppress_s3_errors: bool = False,
    describe_func: DescribeWorkflow = describe_workflow,
    settings: Settings | None = None,
) -> WorkflowRunSyncResult:
    """Poll Seqera for one run and persist final status/result metadata when available."""
    if not run.seqera_run_id:
        return WorkflowRunSyncResult(
            run_id=run.id,
            seqera_run_id=None,
            skipped=True,
        )

    if not force and run.is_fully_synced():
        status = _normalize_status(run.seqera_final_status)
        return WorkflowRunSyncResult(
            run_id=run.id,
            seqera_run_id=run.seqera_run_id,
            seqera_status=status,
            ui_status=_map_status_to_ui(status),
            terminal=True,
            sync_completed=True,
            skipped=True,
        )

    status = _normalize_status(run.seqera_final_status)
    if force or status not in TERMINAL_SEQERA_STATUSES:
        if describe_func is describe_workflow:
            payload = await describe_workflow(run.seqera_run_id, settings=settings)
        else:
            payload = await describe_func(run.seqera_run_id)
        status = _normalize_status(extract_pipeline_status(payload))
        if status not in TERMINAL_SEQERA_STATUSES:
            return WorkflowRunSyncResult(
                run_id=run.id,
                seqera_run_id=run.seqera_run_id,
                seqera_status=status,
                ui_status=_map_status_to_ui(status),
                terminal=False,
            )

        run.seqera_final_status = status
        db.add(run)
        db.commit()
        db.refresh(run)

    outputs_synced = 0
    seqera_completed = status == PipelineStatus.SUCCEEDED.value
    sync_incomplete = run.sync_completed_at is None
    if seqera_completed and (sync_incomplete or force):
        outputs_synced = await _sync_completed_run_results(
            db,
            run,
            suppress_s3_errors=suppress_s3_errors,
            settings=settings,
        )

    if sync_incomplete or force:
        run.sync_completed_at = datetime.now(tz=UTC)
        db.add(run)
        db.commit()

    return WorkflowRunSyncResult(
        run_id=run.id,
        seqera_run_id=run.seqera_run_id,
        seqera_status=status,
        ui_status=_map_status_to_ui(status),
        terminal=True,
        sync_completed=True,
        outputs_synced=outputs_synced,
    )


async def _sync_completed_run_results(
    db: Session,
    run: WorkflowRun,
    *,
    suppress_s3_errors: bool,
    settings: Settings | None = None,
) -> int:
    try:
        spec = get_output_spec(run)
    except ValueError as exc:
        logger.warning("Skipping result sync for run %s: %s", run.id, exc)
        return 0

    if settings is None:
        synced_keys = await sync_workflow_outputs(
            db,
            run=run,
            spec=spec,
            suppress_s3_errors=suppress_s3_errors,
        )
        await ensure_completed_run_score(db, run, UIStatus.COMPLETED.value)
        await sync_service_usage(db, run, UIStatus.COMPLETED.value)
    else:
        synced_keys = await sync_workflow_outputs(
            db,
            run=run,
            spec=spec,
            suppress_s3_errors=suppress_s3_errors,
            settings=settings,
        )
        await ensure_completed_run_score(
            db, run, UIStatus.COMPLETED.value, settings=settings
        )
        await sync_service_usage(db, run, UIStatus.COMPLETED.value, settings=settings)
    return len(synced_keys)


def _normalize_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().upper()
    return normalized or None


def _map_status_to_ui(status: str | None) -> str | None:
    if status is None:
        return None
    return map_pipeline_status_to_ui(status)


def _error_result(run: WorkflowRun, exc: Exception) -> WorkflowRunSyncResult:
    return WorkflowRunSyncResult(
        run_id=run.id,
        seqera_run_id=run.seqera_run_id,
        seqera_status=_normalize_status(run.seqera_final_status),
        ui_status=_map_status_to_ui(_normalize_status(run.seqera_final_status)),
        terminal=_normalize_status(run.seqera_final_status) in TERMINAL_SEQERA_STATUSES,
        sync_completed=run.sync_completed_at is not None,
        error=str(exc),
    )
