"""Workflow-related HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..schemas.workflows import (
    DatasetUploadRequest,
    DatasetUploadResponse,
    JobListItem,
    JobListResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
    map_pipeline_status_to_ui,
)
from ..services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
    launch_bindflow_workflow,
)
from ..services.datasets import (
    create_seqera_dataset,
    upload_dataset_to_seqera,
)
from ..services.seqera import (
    SeqeraAPIError,
    SeqeraConfigurationError,
    describe_workflow,
)
from ..services.job_utils import (
    coerce_workflow_payload,
    ensure_completed_run_score,
    extract_pipeline_status,
    get_owned_run,
    get_owned_run_ids,
    get_score_by_seqera_run_id,
    get_workflow_type_by_seqera_run_id,
    parse_submit_datetime,
)
from .dependencies import get_current_user_id, get_db

router = APIRouter(tags=["workflows"])


@router.post("/launch", response_model=WorkflowLaunchResponse, status_code=status.HTTP_201_CREATED)
async def launch_workflow(payload: WorkflowLaunchPayload) -> WorkflowLaunchResponse:
    """Launch a workflow on the Seqera Platform."""
    try:
        dataset_id = payload.datasetId

        # Use the dataset created from /datasets/upload endpoint
        result: BindflowLaunchResult = await launch_bindflow_workflow(payload.launch, dataset_id)
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return WorkflowLaunchResponse(
        message="Workflow launched successfully",
        runId=result.workflow_id,
        status=result.status,
        submitTime=datetime.now(timezone.utc),
    )


@router.get("/runs", response_model=ListRunsResponse)
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    workspace: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ListRunsResponse:
    """List workflow runs (placeholder until Seqera list API integration)."""
    _ = (status_filter, workspace)  # Reserved for future Seqera integration
    return ListRunsResponse(runs=[], total=0, limit=limit, offset=offset)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    search: str | None = Query(None, description="Search by job name or workflow type"),
    status_filter: list[str] | None = Query(None, alias="status", description="Filter by status (Completed, Stopped, Failed)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> JobListResponse:
    """
    Retrieve a paginated list of the current user's jobs with search and filtering.
    Requires authentication via Bearer token.
    
    Query Parameters:
    - search: Search by job name or workflow type
    - status: Filter by status values (Completed, Stopped, Failed, In progress, In queue)
    - limit: Maximum number of results per page
    - offset: Number of results to skip for pagination
    
    Returns:
    - Paginated list of jobs owned by the authenticated user
    """
    try:
        # Get only the workflows owned by the current user
        owned_run_ids = get_owned_run_ids(db, current_user_id)
        score_by_run_id = get_score_by_seqera_run_id(db, current_user_id)
        workflow_type_by_run_id = get_workflow_type_by_seqera_run_id(db, current_user_id)
        
        search_text = (search or "").strip().lower()
        allowed_statuses = set(status_filter or [])
        
        jobs: list[JobListItem] = []
        
        for run_id in owned_run_ids:
            # Fetch workflow details from Seqera
            payload = await describe_workflow(run_id)
            wf = coerce_workflow_payload(payload)
            pipeline_status = extract_pipeline_status(payload)
            ui_status = map_pipeline_status_to_ui(pipeline_status)
            
            # Apply status filter
            if allowed_statuses and ui_status not in allowed_statuses:
                continue
            
            # Get workflow type and job name
            workflow_type = workflow_type_by_run_id.get(run_id)
            job_name = wf.get("runName") or run_id
            
            # Apply search filter
            if (
                search_text
                and search_text not in str(job_name).lower()
                and search_text not in str(workflow_type or "").lower()
            ):
                continue
            
            # Get or compute score
            score = score_by_run_id.get(run_id)
            owned_run = get_owned_run(db, current_user_id, run_id)
            if score is None and owned_run:
                score = await ensure_completed_run_score(db, owned_run, ui_status)
            
            # Parse submission date
            submitted_at = parse_submit_datetime(wf.get("submit")) or datetime.now(timezone.utc)
            
            jobs.append(
                JobListItem(
                    id=run_id,
                    jobName=job_name,
                    workflowType=workflow_type,
                    status=ui_status,
                    submittedAt=submitted_at,
                    score=score,
                )
            )
        
        # Apply pagination
        total = len(jobs)
        paginated_jobs = jobs[offset : offset + limit]
        
        return JobListResponse(
            jobs=paginated_jobs,
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


@router.get("/{run_id}/logs", response_model=LaunchLogs)
async def get_logs() -> LaunchLogs:
    """Retrieve workflow logs (placeholder)."""
    return LaunchLogs(
        truncated=False,
        entries=[],
        rewindToken="",
        forwardToken="",
        pending=False,
        message="Logs endpoint - implementation pending",
        downloads=[],
    )


@router.get("/{run_id}/details", response_model=LaunchDetails)
async def get_details(run_id: str) -> LaunchDetails:
    """Return workflow details (placeholder)."""
    iso_now = datetime.now(timezone.utc).isoformat()
    return LaunchDetails(
        requiresAttention=False,
        status="UNKNOWN",
        ownerId=0,
        repository="",
        id=run_id,
        submit="",
        start="",
        complete="",
        dateCreated=iso_now,
        lastUpdated=iso_now,
        runName="",
        sessionId="",
        profile="",
        workDir="",
        commitId="",
        userName="",
        scriptId="",
        revision="",
        commandLine="",
        projectName="",
        scriptName="",
        launchId="",
        configFiles=[],
        params={},
    )


@router.post("/datasets/upload", response_model=DatasetUploadResponse)
async def upload_dataset(payload: DatasetUploadRequest) -> DatasetUploadResponse:
    """Create a Seqera dataset and upload form data as CSV content."""
    try:
        dataset = await create_seqera_dataset(
            name=payload.datasetName, description=payload.datasetDescription
        )
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Allow Seqera time to finish dataset initialization before uploading
    await asyncio.sleep(2)

    try:
        upload_result = await upload_dataset_to_seqera(dataset.dataset_id, payload.formData)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return DatasetUploadResponse(
        message="Dataset created and uploaded successfully",
        datasetId=upload_result.dataset_id,
        success=upload_result.success,
        details=upload_result.raw_response,
    )
