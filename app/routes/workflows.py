"""Workflow-related HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models.core import Workflow, WorkflowRun
from ..schemas.workflows import (
    DatasetUploadRequest,
    DatasetUploadResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
)
from ..services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
    _get_required_env,
    launch_bindflow_workflow,
)
from ..services.datasets import (
    create_seqera_dataset,
    upload_dataset_to_seqera,
)
from .dependencies import get_current_user_id, get_db

router = APIRouter(tags=["workflows"])


@router.post("/me/sync")
async def sync_current_user(
    current_user_id: UUID = Depends(get_current_user_id),
) -> dict[str, str]:
    """Ensure authenticated user exists in app_users and return user id."""
    return {"message": "User synced", "userId": str(current_user_id)}


@router.post("/launch", response_model=WorkflowLaunchResponse, status_code=status.HTTP_201_CREATED)
async def launch_workflow(
    payload: WorkflowLaunchPayload,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> WorkflowLaunchResponse:
    """Launch a workflow on the Seqera Platform."""
    requested_tool_raw = payload.launch.tool
    requested_tool = requested_tool_raw.strip().lower()
    if requested_tool != "bindcraft":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                f"Tool '{requested_tool_raw}' is not available for workflow launch yet. "
                "Only BindCraft is supported at the moment."
            ),
        )

    dataset_id = payload.datasetId.strip()
    if not dataset_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="datasetId is required and must not be empty.",
        )

    run_name = payload.launch.runName
    workflow = db.scalar(select(Workflow).where(func.lower(Workflow.name) == requested_tool))
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Workflow '{requested_tool_raw}' is not configured in workflows table. "
                "Seed the workflows catalog before launching."
            ),
        )

    if not workflow.repo_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workflow '{workflow.name}' is missing repo_url in workflows table.",
        )

    if not workflow.default_revision:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workflow '{workflow.name}' is missing default_revision in workflows table.",
        )
    resolved_revision = workflow.default_revision

    run_id = uuid4()
    base_work_dir = _get_required_env("WORK_DIR").rstrip("/")
    run_work_dir = f"{base_work_dir}/{run_id}"

    # Reserve DB row first so a launched workflow always has a DB entry.
    # Use local run UUID as a temporary seqera_run_id placeholder.
    workflow_run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        owner_user_id=current_user_id,
        seqera_dataset_id=payload.datasetId,
        seqera_run_id=str(run_id),
        run_name=run_name,
        work_dir=run_work_dir,
    )

    db.add(workflow_run)
    db.commit()

    try:
        result: BindflowLaunchResult = await launch_bindflow_workflow(
            payload.launch,
            dataset_id,
            pipeline=workflow.repo_url,
            revision=resolved_revision,
            output_id=str(run_id),
        )
        workflow_run.seqera_run_id = result.workflow_id
        db.commit()
    except BindflowConfigurationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update local workflow run after launch.",
        ) from exc

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
    _ = (status_filter, workspace)
    return ListRunsResponse(runs=[], total=0, limit=limit, offset=offset)


@router.get("/{run_id}/logs", response_model=LaunchLogs)
async def get_logs(run_id: str) -> LaunchLogs:
    """Retrieve workflow logs (placeholder)."""
    _ = run_id
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
async def upload_dataset(
    payload: DatasetUploadRequest,
    current_user_id: UUID = Depends(get_current_user_id),
) -> DatasetUploadResponse:
    """Create a Seqera dataset and upload form data as CSV content."""
    _ = current_user_id
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
