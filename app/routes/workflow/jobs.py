"""Job listing/detail/deletion endpoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ...db.models.core import DataTransfer, RunInput, RunMetric, RunOutput, WorkflowRun
from ...schemas.workflows.shared import (
    TERMINAL_SEQERA_STATUSES,
    BulkDeleteJobsRequest,
    BulkDeleteJobsResponse,
    CancelWorkflowResponse,
    DeleteJobResponse,
    JobDetailsResponse,
    JobListItem,
    JobListResponse,
    map_pipeline_status_to_ui,
)
from ...services.job_utils import (
    UserJobListRow,
    coerce_workflow_payload,
    ensure_completed_run_score,
    extract_pipeline_status,
    format_tool_name,
    format_workflow_name,
    get_owned_run_by_id,
    get_user_job_list_rows,
    parse_submit_datetime,
    sync_service_usage,
)
from ...services.seqera import describe_workflow
from ...services.seqera_client import cancel_workflow_raw, delete_workflow_raw, delete_workflows_raw
from ...services.seqera_errors import SeqeraAPIError, SeqeraConfigurationError
from ..dependencies import get_current_user_id, get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"], dependencies=[Depends(get_current_user_id)])


def _resolve_job_name(run_id: str, wf: dict[str, object], owned_run: WorkflowRun | None) -> str:
    if owned_run is not None:
        for attr in ("binder_name", "run_name"):
            value = getattr(owned_run, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    run_name = wf.get("runName")
    if isinstance(run_name, str) and run_name.strip():
        return run_name.strip()
    return run_id


def _resolve_final_design_count(owned_run: WorkflowRun | None) -> int | None:
    if not owned_run or not owned_run.metrics:
        return None
    value = owned_run.metrics.final_design_count
    return value if isinstance(value, int) else None


def _resolve_stored_score(owned_run: WorkflowRun | None) -> float | None:
    if not owned_run or not owned_run.metrics:
        return None
    value = owned_run.metrics.max_score
    if value is None:
        return None
    if isinstance(value, (float, int, Decimal)):
        return round(float(value), 3)
    return None


def _get_stored_terminal_ui_status(run: WorkflowRun) -> str | None:
    if run.seqera_final_status is None:
        return None
    status = run.seqera_final_status.strip().upper()
    if status not in TERMINAL_SEQERA_STATUSES:
        return None
    return map_pipeline_status_to_ui(status)


@router.post("/{run_id}/cancel", response_model=CancelWorkflowResponse)
async def cancel_workflow(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CancelWorkflowResponse:
    """Cancel a pending or running workflow run."""
    owned_run = get_owned_run_by_id(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    queued_job = owned_run.get_queued_job(session=db)
    if queued_job and queued_job.status == "pending":
        queued_job.cancel_pending_job(session=db)

    if owned_run.seqera_run_id is not None:
        try:
            await cancel_workflow_raw(owned_run.seqera_run_id, settings=settings)
        except SeqeraAPIError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    db.commit()
    return CancelWorkflowResponse(
        message="Workflow cancelled successfully",
        runId=run_id,
        status="cancelled",
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(
    search: str | None = Query(None, description="Search by job name or workflow type"),
    status_filter: list[str] | None = Query(
        None,
        alias="status",
        description="Filter by status (Completed, Stopped, Failed)",
    ),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobListResponse:
    """Retrieve a paginated list of the current user's jobs with search and filtering."""
    user_runs = get_user_job_list_rows(db, current_user_id)
    search_text = (search or "").strip().lower()
    allowed_statuses = set(status_filter or [])

    async def _fetch_seqera(user_run: UserJobListRow) -> tuple[str, dict[str, object] | None]:
        """None signals a 4xx (skip); empty dict signals a 5xx/network error (DB fallback)."""
        seqera_run_id = user_run.seqera_run_id
        if not seqera_run_id:
            return user_run.run_id, {}
        try:
            return user_run.run_id, await describe_workflow(seqera_run_id, settings=settings)
        except SeqeraAPIError as exc:
            if exc.status_code is not None and exc.status_code < 500:
                return user_run.run_id, None
            logger.warning(
                "Seqera unavailable for run %s, using DB fallback: %s",
                seqera_run_id,
                exc,
            )
            return user_run.run_id, {}
        except Exception as exc:
            logger.warning(
                "Seqera unreachable for run %s, using DB fallback: %s",
                seqera_run_id,
                exc,
            )
            return user_run.run_id, {}

    # Only need to get status from seqera for runs
    #   that are currently running
    seqera_fetch_rows = [
        user_run
        for user_run in user_runs
        if user_run.seqera_run_id
        and user_run.queued_status not in {"pending", "failed"}
        and not user_run.run.is_seqera_finalized()
    ]
    seqera_results = dict(
        await asyncio.gather(*(_fetch_seqera(user_run) for user_run in seqera_fetch_rows))
    )

    jobs: list[JobListItem] = []
    seqera_unavailable = False

    for user_run in user_runs:
        run_id = user_run.run_id
        owned_run = user_run.run
        seqera_run_id = user_run.seqera_run_id

        seqera_payload: dict[str, object] | None = None
        ui_status = "N/A"
        if user_run.queued_status == "pending":
            ui_status = "Pending"
        elif user_run.queued_status == "failed":
            ui_status = "Failed"
        elif stored_ui_status := _get_stored_terminal_ui_status(owned_run):
            ui_status = stored_ui_status
        elif seqera_run_id:
            fetched_payload = seqera_results.get(run_id)
            if fetched_payload is None:
                # 4xx: run is inaccessible (not found, wrong workspace, no permission).
                continue
            if fetched_payload:
                seqera_payload = fetched_payload
                pipeline_status = extract_pipeline_status(fetched_payload)
                ui_status = map_pipeline_status_to_ui(pipeline_status)
            else:
                seqera_unavailable = True

        if allowed_statuses and ui_status not in allowed_statuses:
            continue

        wf = coerce_workflow_payload(seqera_payload or {})
        submitted_at = (
            parse_submit_datetime(seqera_payload or {})
            or owned_run.submission_timestamp
            or datetime.now(UTC)
        )

        workflow_type = user_run.workflow_type
        tool = user_run.tool
        job_name = _resolve_job_name(run_id, wf, owned_run)

        if (
            search_text
            and search_text not in str(job_name).lower()
            and search_text not in str(workflow_type or "").lower()
        ):
            continue

        db_score = user_run.score

        # A cached score means the job completed at some point; treat it as Completed
        # when Seqera is unreachable and we cannot get the live status.
        if ui_status == "N/A" and db_score is not None:
            ui_status = "Completed"

        score = db_score
        sync_incomplete = owned_run.sync_completed_at is None
        if score is None and sync_incomplete:
            score = await ensure_completed_run_score(
                db, owned_run, ui_status, settings=settings
            )

        # Keep request-time sync as a fallback only until the scheduler has
        # completed result syncing for this run.
        if sync_incomplete and owned_run.service_usage is None:
            await sync_service_usage(
                db, run=owned_run, ui_status=ui_status, settings=settings
            )

        jobs.append(
            JobListItem(
                id=run_id,
                seqeraRunId=seqera_run_id,
                jobName=job_name,
                workflow=workflow_type,
                tool=tool,
                status=ui_status,
                submittedAt=submitted_at,
                score=score if ui_status == "Completed" else None,
                finalDesignCount=user_run.final_design_count,
            )
        )

    jobs.sort(key=lambda item: item.submittedAt, reverse=True)
    total = len(jobs)
    jobs = jobs[offset : offset + limit]

    return JobListResponse(
        jobs=jobs,
        total=total,
        limit=limit,
        offset=offset,
        seqeraUnavailable=seqera_unavailable,
    )


@router.get("/{run_id}", response_model=JobDetailsResponse)
async def get_job_details(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobDetailsResponse:
    """Retrieve a single job with normalized status and score."""
    owned_run = get_owned_run_by_id(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    stored_ui_status = _get_stored_terminal_ui_status(owned_run)
    if not owned_run.seqera_run_id and stored_ui_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Seqera run ID not available"
        )

    seqera_payload: dict[str, object] | None = None
    if stored_ui_status is None:
        seqera_run_id = owned_run.seqera_run_id
        if seqera_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Seqera run ID not available"
            )
        try:
            seqera_payload = await describe_workflow(seqera_run_id, settings=settings)
        except SeqeraConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
        except SeqeraAPIError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    wf = coerce_workflow_payload(seqera_payload or {})
    ui_status = stored_ui_status
    if ui_status is None and seqera_payload is not None:
        pipeline_status = extract_pipeline_status(seqera_payload)
        ui_status = map_pipeline_status_to_ui(pipeline_status)
    if ui_status is None:
        ui_status = "N/A"
    submitted_at = (
        parse_submit_datetime(seqera_payload or {})
        or owned_run.submission_timestamp
        or datetime.now(UTC)
    )

    score = None
    if ui_status == "Completed":
        score = _resolve_stored_score(owned_run)
        sync_incomplete = owned_run.sync_completed_at is None
        if score is None and sync_incomplete:
            score = await ensure_completed_run_score(
                db, owned_run, ui_status, settings=settings
            )
        if sync_incomplete and owned_run.service_usage is None:
            await sync_service_usage(
                db, run=owned_run, ui_status=ui_status, settings=settings
            )

    raw_tool: str | None = getattr(owned_run, "tool", None) or None
    if not raw_tool:
        form_data = owned_run.submitted_form_data
        if isinstance(form_data, dict):
            for _key in ("tool", "mode"):
                _raw = form_data.get(_key)
                if _raw and str(_raw).strip():
                    raw_tool = str(_raw).strip()
                    break
    tool = format_tool_name(raw_tool) if raw_tool else "Unknown"
    return JobDetailsResponse(
        id=run_id,
        jobName=_resolve_job_name(run_id, wf, owned_run),
        workflow=(
            format_workflow_name(owned_run.workflow.name) if owned_run.workflow else "Unknown"
        ),
        tool=tool,
        status=ui_status,
        submittedAt=submitted_at,
        score=score,
        finalDesignCount=_resolve_final_design_count(owned_run),
    )


@router.delete("/{run_id}", response_model=DeleteJobResponse)
async def delete_job(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DeleteJobResponse:
    """Delete a single job. Running jobs are cancelled before deletion."""
    owned_run = get_owned_run_by_id(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    cancelled = False
    seqera_run_id = owned_run.seqera_run_id
    # Cancel the queued job if it's still pending.
    queued_job = owned_run.get_queued_job(session=db)
    if queued_job and queued_job.status == "pending":
        queued_job.cancel_pending_job(session=db)
    if seqera_run_id:
        try:
            payload = await describe_workflow(seqera_run_id, settings=settings)
            pipeline_status = extract_pipeline_status(payload)
            if pipeline_status in {"SUBMITTED", "RUNNING"}:
                await cancel_workflow_raw(seqera_run_id, settings=settings)
                cancelled = True

            await delete_workflow_raw(seqera_run_id, settings=settings)
        except SeqeraConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
        except SeqeraAPIError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    db.execute(delete(RunMetric).where(RunMetric.run_id == owned_run.id))
    db.execute(delete(RunInput).where(RunInput.run_id == owned_run.id))
    db.execute(delete(RunOutput).where(RunOutput.run_id == owned_run.id))
    db.execute(delete(DataTransfer).where(DataTransfer.workflow_run_id == owned_run.id))
    if queued_job:
        db.delete(queued_job)
    db.delete(owned_run)
    db.commit()

    return DeleteJobResponse(
        runId=run_id,
        deleted=True,
        cancelledBeforeDelete=cancelled,
        message="Job deleted successfully",
    )


@router.post("/bulk-delete", response_model=BulkDeleteJobsResponse)
async def bulk_delete_jobs(
    payload: BulkDeleteJobsRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BulkDeleteJobsResponse:
    """Delete multiple jobs. Each running job is cancelled before deletion."""
    deleted: list[str] = []
    failed: dict[str, str] = {}
    status: dict = {}

    for run_id in payload.runIds:
        owned_run = get_owned_run_by_id(db, current_user_id, run_id)
        if not owned_run:
            failed[run_id] = "Job not found"
            continue
        run_status: dict[str, Any] = {"run": owned_run}

        queued_job = owned_run.get_queued_job(session=db)
        if queued_job:
            run_status["queued_job"] = queued_job
            if queued_job.status == "pending":
                queued_job.cancel_pending_job(session=db)
                run_status["queue_cancelled"] = True

        if owned_run.seqera_run_id is not None:
            try:
                details = await describe_workflow(owned_run.seqera_run_id, settings=settings)
                if extract_pipeline_status(details) in {"SUBMITTED", "RUNNING"}:
                    await cancel_workflow_raw(owned_run.seqera_run_id, settings=settings)
                run_status["seqera_cancelled"] = True
                run_status["seqera_id"] = owned_run.seqera_run_id
            except (SeqeraConfigurationError, SeqeraAPIError) as exc:
                failed[run_id] = str(exc)

        status[run_id] = run_status

    delete_from_seqera = [
        (run_id, run_status)
        for run_id, run_status in status.items()
        if run_status.get("seqera_cancelled") and run_status.get("seqera_id")
    ]
    if delete_from_seqera:
        try:
            seqera_ids = [run_status["seqera_id"] for run_id, run_status in delete_from_seqera]
            await delete_workflows_raw(seqera_ids, settings=settings)
        except (SeqeraConfigurationError, SeqeraAPIError) as exc:
            for run_id, _run_status in delete_from_seqera:
                failed[run_id] = str(exc)

    delete_from_db = [
        run_id
        for run_id, run_status in status.items()
        if run_status.get("queued_job") or run_status.get("seqera_cancelled")
    ]
    for run_id in delete_from_db:
        # Don't delete if Seqera deletion failed.
        if run_id in failed:
            continue
        run = status[run_id]["run"]
        try:
            db.execute(delete(RunMetric).where(RunMetric.run_id == run.id))
            db.execute(delete(RunInput).where(RunInput.run_id == run.id))
            db.execute(delete(RunOutput).where(RunOutput.run_id == run.id))
            db.execute(delete(DataTransfer).where(DataTransfer.workflow_run_id == run.id))
            if queued_job := run.get_queued_job(session=db):
                db.delete(queued_job)
            db.delete(run)
            db.commit()
            deleted.append(run_id)
        except Exception as exc:  # pragma: no cover - unexpected DB failures
            db.rollback()
            failed[run_id] = str(exc)

    return BulkDeleteJobsResponse(deleted=deleted, failed=failed)
