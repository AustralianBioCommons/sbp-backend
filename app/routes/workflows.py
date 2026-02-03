"""Workflow-related HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..db.models.core import AppUser, WorkflowRun

from ..schemas.workflows import (
    CancelWorkflowResponse,
    DatasetUploadRequest,
    DatasetUploadResponse,
    JobListItem,
    JobListResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    RunInfo,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
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
    list_seqera_workflows,
)

router = APIRouter(tags=["workflows"])


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user_id(
    x_auth0_user_id: str | None = Header(None, alias="X-Auth0-User-Id"),
    db: Session = Depends(get_db),
) -> UUID:
    if not x_auth0_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Auth0-User-Id header",
        )

    user = db.execute(
        select(AppUser).where(AppUser.auth0_user_id == x_auth0_user_id)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
        )
    return user.id


def get_owned_run_ids(db: Session, user_id: UUID) -> set[str]:
    rows = db.execute(
        select(WorkflowRun.seqera_run_id).where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {row[0] for row in rows if row[0]}


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


@router.post("/{run_id}/cancel", response_model=CancelWorkflowResponse)
async def cancel_workflow(run_id: str) -> CancelWorkflowResponse:
    """Cancel a workflow run (placeholder implementation)."""
    return CancelWorkflowResponse(
        message="Workflow cancelled successfully",
        runId=run_id,
        status="cancelled",
    )


@router.get("/runs", response_model=ListRunsResponse)
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    workspace: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ListRunsResponse:
    """List workflow runs with a compact response shape for the frontend."""
    parsed_status_filter = None
    if status_filter:
        parsed_status_filter = [item.strip() for item in status_filter.split(",") if item.strip()]

    try:
        workflows, _total = await list_seqera_workflows(
            workspace_id=workspace,
            status_filter=parsed_status_filter,
            limit=limit,
            offset=offset,
        )

        owned_run_ids = get_owned_run_ids(db, current_user_id)
        owned_workflows = [wf for wf in workflows if wf.workflow_id in owned_run_ids]

        runs = [
            RunInfo(
                id=wf.workflow_id,
                run=wf.run_name or wf.workflow_id,
                workflow=wf.workflow_type or "",
                status=wf.ui_status,
                date=wf.submitted_at.isoformat() if wf.submitted_at else "",
                cancel=f"/api/workflows/{wf.workflow_id}/cancel",
            )
            for wf in owned_workflows
        ]
        return ListRunsResponse(runs=runs, total=len(runs), limit=limit, offset=offset)
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


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    search: str | None = Query(None, description="Search by job name or workflow type"),
    status: list[str] | None = Query(None, description="Filter by status (Completed, Stopped, Failed)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> JobListResponse:
    """
    Retrieve a paginated list of workflow jobs with search and filtering.
    
    Query Parameters:
    - search: Search by job name or workflow type
    - status: Filter by status values (Completed, Stopped, Failed, In progress, In queue)
    - limit: Maximum number of results per page
    - offset: Number of results to skip for pagination
    
    Returns:
    - Paginated list of jobs with job name, workflow type, status, submitted date, and score
    """
    try:
        workflows, _total = await list_seqera_workflows(
            search_query=search,
            status_filter=status,
            limit=limit,
            offset=offset,
        )

        owned_run_ids = get_owned_run_ids(db, current_user_id)
        owned_workflows = [wf for wf in workflows if wf.workflow_id in owned_run_ids]
        
        jobs = [
            JobListItem(
                id=wf.workflow_id,
                jobName=wf.run_name or wf.workflow_id,
                workflowType=wf.workflow_type,
                status=wf.ui_status,
                submittedAt=wf.submitted_at or datetime.now(timezone.utc),
                score=wf.score,
            )
            for wf in owned_workflows
        ]
        
        return JobListResponse(
            jobs=jobs,
            total=len(jobs),
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
