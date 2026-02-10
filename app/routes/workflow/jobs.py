"""Job listing/detail/deletion endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ...db.models.core import RunInput, RunMetric, RunOutput, WorkflowRun
from ...schemas.workflows import (
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
    coerce_workflow_payload,
    ensure_completed_run_score,
    extract_pipeline_status,
    get_owned_run,
    get_owned_run_ids,
    get_score_by_seqera_run_id,
    get_workflow_type_by_seqera_run_id,
    parse_submit_datetime,
)
from ...services.seqera import (
    SeqeraAPIError,
    SeqeraConfigurationError,
    cancel_seqera_workflow,
    delete_seqera_workflow,
    delete_seqera_workflows,
    describe_workflow,
)
from ..dependencies import get_current_user_id, get_db

router = APIRouter()


@router.post("/{run_id}/cancel", response_model=CancelWorkflowResponse)
async def cancel_workflow(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> CancelWorkflowResponse:
    """Cancel a workflow run."""
    owned_run = get_owned_run(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    try:
        await cancel_seqera_workflow(run_id)
    except SeqeraAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return CancelWorkflowResponse(
        message="Workflow cancelled successfully",
        runId=run_id,
        status="cancelled",
    )


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    search: str | None = Query(None, description="Search by job name or workflow type"),
    status_filter: list[str]
    | None = Query(
        None,
        alias="status",
        description="Filter by status (Completed, Stopped, Failed)",
    ),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> JobListResponse:
    """Retrieve a paginated list of the current user's jobs with search and filtering."""
    try:
        owned_run_ids = get_owned_run_ids(db, current_user_id)
        score_by_run_id = get_score_by_seqera_run_id(db, current_user_id)
        workflow_type_by_run_id = get_workflow_type_by_seqera_run_id(db, current_user_id)
        search_text = (search or "").strip().lower()
        allowed_statuses = set(status_filter or [])
        jobs: list[JobListItem] = []
        for run_id in owned_run_ids:
            payload = await describe_workflow(run_id)
            wf = coerce_workflow_payload(payload)
            pipeline_status = extract_pipeline_status(payload)
            ui_status = map_pipeline_status_to_ui(pipeline_status)

            if allowed_statuses and ui_status not in allowed_statuses:
                continue

            workflow_type = workflow_type_by_run_id.get(run_id)
            job_name = wf.get("runName") or run_id
            if (
                search_text
                and search_text not in str(job_name).lower()
                and search_text not in str(workflow_type or "").lower()
            ):
                continue

            score = score_by_run_id.get(run_id)
            owned_run = get_owned_run(db, current_user_id, run_id)
            if score is None and owned_run:
                score = await ensure_completed_run_score(db, owned_run, ui_status)

            jobs.append(
                JobListItem(
                    id=run_id,
                    jobName=job_name,
                    workflowType=workflow_type,
                    status=ui_status,
                    submittedAt=parse_submit_datetime(payload) or datetime.now(timezone.utc),
                    score=score if ui_status == "Completed" else None,
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
        )
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except SeqeraAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.get("/jobs/{run_id}", response_model=JobDetailsResponse)
async def get_job_details(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> JobDetailsResponse:
    """Retrieve a single job with normalized status and score."""
    owned_run = get_owned_run(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    try:
        payload = await describe_workflow(run_id)
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    wf = coerce_workflow_payload(payload)
    pipeline_status = extract_pipeline_status(payload)
    ui_status = map_pipeline_status_to_ui(pipeline_status)
    submitted_at = parse_submit_datetime(payload) or datetime.now(timezone.utc)

    score = await ensure_completed_run_score(db, owned_run, ui_status)
    if ui_status != "Completed":
        score = None

    return JobDetailsResponse(
        id=run_id,
        jobName=wf.get("runName") or run_id,
        workflowType=(owned_run.workflow.name if owned_run.workflow else None),
        status=ui_status,
        submittedAt=submitted_at,
        score=score,
    )


@router.delete("/jobs/{run_id}", response_model=DeleteJobResponse)
async def delete_job(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> DeleteJobResponse:
    """Delete a single job. Running jobs are cancelled before deletion."""
    owned_run = get_owned_run(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    cancelled = False
    try:
        payload = await describe_workflow(run_id)
        pipeline_status = extract_pipeline_status(payload)
        if pipeline_status in {"SUBMITTED", "RUNNING"}:
            await cancel_seqera_workflow(run_id)
            cancelled = True

        await delete_seqera_workflow(run_id)
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    db.execute(delete(RunMetric).where(RunMetric.run_id == owned_run.id))
    db.execute(delete(RunInput).where(RunInput.run_id == owned_run.id))
    db.execute(delete(RunOutput).where(RunOutput.run_id == owned_run.id))
    db.delete(owned_run)
    db.commit()

    return DeleteJobResponse(
        runId=run_id,
        deleted=True,
        cancelledBeforeDelete=cancelled,
        message="Job deleted successfully",
    )


@router.post("/jobs/bulk-delete", response_model=BulkDeleteJobsResponse)
async def bulk_delete_jobs(
    payload: BulkDeleteJobsRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> BulkDeleteJobsResponse:
    """Delete multiple jobs. Each running job is cancelled before deletion."""
    deleted: list[str] = []
    failed: dict[str, str] = {}
    to_delete: list[tuple[str, WorkflowRun]] = []

    for run_id in payload.runIds:
        owned_run = get_owned_run(db, current_user_id, run_id)
        if not owned_run:
            failed[run_id] = "Job not found"
            continue

        try:
            details = await describe_workflow(run_id)
            if extract_pipeline_status(details) in {"SUBMITTED", "RUNNING"}:
                await cancel_seqera_workflow(run_id)
            to_delete.append((run_id, owned_run))
        except (SeqeraConfigurationError, SeqeraAPIError) as exc:
            db.rollback()
            failed[run_id] = str(exc)

    if to_delete:
        run_ids = [run_id for run_id, _ in to_delete]
        try:
            await delete_seqera_workflows(run_ids)
        except (SeqeraConfigurationError, SeqeraAPIError) as exc:
            for run_id in run_ids:
                failed[run_id] = str(exc)
            return BulkDeleteJobsResponse(deleted=deleted, failed=failed)

        for run_id, owned_run in to_delete:
            try:
                db.execute(delete(RunMetric).where(RunMetric.run_id == owned_run.id))
                db.execute(delete(RunInput).where(RunInput.run_id == owned_run.id))
                db.execute(delete(RunOutput).where(RunOutput.run_id == owned_run.id))
                db.delete(owned_run)
                db.commit()
                deleted.append(run_id)
            except Exception as exc:  # pragma: no cover - unexpected DB failures
                db.rollback()
                failed[run_id] = str(exc)

    return BulkDeleteJobsResponse(deleted=deleted, failed=failed)
