"""Workflow-related HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models.core import RunMetric, Workflow, WorkflowRun
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


def _extract_form_id(form_data: dict[str, object] | None) -> str | None:
    if not isinstance(form_data, dict):
        return None
    for key in ("id", "sample_id", "binder_name"):
        value = form_data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_final_design_count(form_data: dict[str, object] | None) -> int | None:
    if not isinstance(form_data, dict):
        return None
    value = form_data.get("number_of_final_designs")
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


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
    form_id = _extract_form_id(payload.formData)
    final_design_count = _extract_final_design_count(payload.formData)
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

    try:
        # Use workflow config from DB (repo_url/default_revision) and selected dataset.
        result: BindflowLaunchResult = await launch_bindflow_workflow(
            payload.launch,
            dataset_id,
            pipeline=workflow.repo_url,
            revision=resolved_revision,
        )
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    base_work_dir = _get_required_env("WORK_DIR").rstrip("/")
    run_id = uuid4()
    run_work_dir = f"{base_work_dir}/{run_id}"
    workflow_run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        owner_user_id=current_user_id,
        seqera_dataset_id=payload.datasetId,
        seqera_run_id=result.workflow_id,
        run_name=run_name,
        work_dir=run_work_dir,
    )
    if form_id:
        workflow_run.binder_name = form_id
        workflow_run.sample_id = form_id
    db.add(workflow_run)
    if final_design_count is not None:
        db.add(RunMetric(run_id=run_id, final_design_count=final_design_count))
    db.commit()

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
