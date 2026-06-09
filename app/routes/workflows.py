"""Workflow-related HTTP routes."""

from __future__ import annotations

import random
import re
import string
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models.core import AppUser, RunMetric, Workflow, WorkflowRun
from ..schemas.workflows import (
    DatasetUploadRequest,
    DatasetUploadResponse,
    InteractionScreeningDatasetUploadRequest,
    InteractionScreeningDatasetUploadResponse,
    InteractionScreeningFormData,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    WorkflowFormData,
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
from ..services.credits import WorkflowCreditsResponse, list_workflow_credit_configs
from ..services.datasets import (
    create_seqera_dataset,
    upload_dataset_to_seqera,
    upload_interaction_screening_dataset,
)
from ..services.proteinfold_executor import (
    ProteinfoldConfigurationError,
    ProteinfoldExecutorError,
    ProteinfoldLaunchResult,
    launch_proteinfold_workflow,
)
from ..services.seqera_errors import SeqeraConfigurationError, SeqeraExecutorError
from ..services.wisps_executor import (
    WispsConfigurationError,
    WispsExecutorError,
    WispsLaunchResult,
    launch_wisps_workflow,
)
from .dependencies import (
    get_client_ip,
    get_current_user_id,
    get_db,
    require_workflow_execution_role,
)

router = APIRouter(
    tags=["workflows"],
    dependencies=[Depends(get_current_user_id), Depends(require_workflow_execution_role)],
)


def build_unique_run_name(job_name: str) -> str:
    # Produces a parseable run name: <slug>_<YYYYMMDD-HHMMSS>_<4-char random>
    # Underscores delimit the three parts; hyphens are only used within slug and timestamp.
    base = job_name.strip()
    slug = re.sub(r"[^a-zA-Z0-9\-]", "-", base)  # underscores → hyphens too
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-") or "run"
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{slug}_{ts}_{rand}"


def build_sample_id(workflow_name: str) -> str:
    """
    Build a sample ID for a workflow run - only needed if one
    wasn't received from the form data.
    """
    chars = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{workflow_name}-{chars}"


def _require_launch_var(name: str, value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"'{name}' is required for workflow launch but could not be determined.",
        )
    return value


def _extract_sample_id(form_data: WorkflowFormData | None) -> str | None:
    """
    sample_id should now be a standard field in the form data - allow
    fallback to old fields if not present.
    """
    if not isinstance(form_data, WorkflowFormData):
        return None
    for key in ("sample_id", "id", "samplesheetId"):
        value = getattr(form_data, key, None)
        if value is None:
            value = form_data.extra_fields.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_binder_name(form_data: WorkflowFormData | None) -> str | None:
    if not isinstance(form_data, WorkflowFormData):
        return None
    value = form_data.extra_fields.get("binder_name")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_final_design_count(form_data: WorkflowFormData | None) -> int | None:
    if not isinstance(form_data, WorkflowFormData):
        return None
    value = form_data.extra_fields.get("number_of_final_designs")
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


@router.get("/credits", response_model=WorkflowCreditsResponse)
async def get_workflow_credits() -> WorkflowCreditsResponse:
    """Return the per-tool credit multipliers for each workflow.

    The frontend uses these to compute the credit cost of a run as
    ``tool_multiplier * quantity`` — see the SBP credit-calculation spec.
    """
    return WorkflowCreditsResponse(workflows=list(list_workflow_credit_configs()))


@router.post(
    "/launch",
    response_model=WorkflowLaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def launch_workflow(
    payload: WorkflowLaunchPayload,
    current_user_id: UUID = Depends(get_current_user_id),
    launch_ip: str | None = Depends(get_client_ip),
    db_session: Session = Depends(get_db),
) -> WorkflowLaunchResponse:
    """Launch a workflow on the Seqera Platform."""
    requested_workflow = payload.launch.workflow.strip().lower()

    dataset_id = payload.datasetId.strip()
    if not dataset_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="datasetId is required and must not be empty.",
        )

    sample_id = _extract_sample_id(payload.formData)
    if sample_id is None:
        sample_id = build_sample_id(requested_workflow)
    binder_name = _extract_binder_name(payload.formData)
    final_design_count = _extract_final_design_count(payload.formData)

    selected_tool = payload.launch.tool or payload.formData.tool
    if not selected_tool:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No tool selected. Provide 'tool' in formData before submitting.",
        )

    # Workflow repo_url and revision come from the DB entry for this workflow name
    # ("single-prediction", "de-novo-design", etc.).
    workflow = db_session.scalar(
        select(Workflow).where(func.lower(Workflow.name) == requested_workflow)
    )
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Workflow '{payload.launch.workflow}' is not configured in workflows table. "
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

    user = db_session.execute(
        select(AppUser.email, AppUser.name).where(AppUser.id == current_user_id)
    ).one_or_none()
    if not user or not user.email:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve user details required for workflow launch.",
        )
    user_email = user.email
    full_name = (user.name or "").replace(" ", "_")
    institute = user_email.split("@")[-1] if "@" in user_email else None
    ip_address: str | None = launch_ip or None

    full_name = _require_launch_var("full_name", full_name)
    institute = _require_launch_var("institute", institute)
    ip_address = _require_launch_var("ip_address", ip_address)

    run_id = uuid4()
    run_work_dir = f"{_get_required_env('WORK_DIR').rstrip('/')}/{run_id}"
    submission_timestamp = datetime.now(timezone.utc)

    # Reserve DB row first so a launched workflow always has a DB entry.
    # Use local run UUID as a temporary seqera_run_id placeholder.
    workflow_run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        owner_user_id=current_user_id,
        seqera_dataset_id=payload.datasetId,
        seqera_run_id=str(run_id),
        binder_name=binder_name,
        sample_id=sample_id,
        run_name=payload.launch.runName,
        submitted_form_data=dict(payload.formData) if payload.formData else None,
        work_dir=run_work_dir,
        launch_ip=launch_ip,
        submission_timestamp=submission_timestamp,
        tool=selected_tool,
    )

    db_session.add(workflow_run)
    if final_design_count is not None:
        db_session.add(RunMetric(run_id=run_id, final_design_count=final_design_count))
    db_session.commit()

    workflow_name = workflow.name.lower()

    # Validate interaction-screening-specific fields before entering the try block
    # so that HTTPException is not swallowed by the generic except Exception handler.
    wisps_form_data: InteractionScreeningFormData | None = None
    if workflow_name == "interaction-screening":
        if not workflow.config_path:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workflow 'interaction-screening' is missing config_path in workflows table.",
            )
        try:
            wisps_form_data = InteractionScreeningFormData.model_validate(
                payload.formData.model_dump()
            )
        except ValidationError as exc:
            missing = next(
                (str(e["loc"][-1]) for e in exc.errors() if e.get("loc")),
                "formData",
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{missing}' is required in formData for interaction-screening.",
            ) from exc

    try:
        result: BindflowLaunchResult | ProteinfoldLaunchResult | WispsLaunchResult
        seqera_run_name = build_unique_run_name(payload.launch.runName or "")
        if workflow_name in ("single-prediction", "proteinfold"):
            # single-prediction → proteinfold executor.
            # selected_tool carries the chosen algorithm ("colabfold", "alphafold2", "boltz").
            tool_algo = selected_tool
            proteinfold_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            result = await launch_proteinfold_workflow(
                proteinfold_launch_form,
                dataset_id,
                pipeline=workflow.repo_url,
                revision=workflow.default_revision,
                output_id=str(run_id),
                mode=tool_algo,
                form_data=payload.formData,
                user_email=user_email,
                full_name=full_name,
                institute=institute,
                ip_address=ip_address,
            )
        elif workflow_name in ("de-novo-design", "bindflow", "bindcraft"):
            # de-novo-design → bindflow executor.
            # selected_tool carries the chosen algorithm ("bindcraft", "boltzgen", "rfdiffusion").
            tool_mode = selected_tool
            bindcraft_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            result = await launch_bindflow_workflow(
                bindcraft_launch_form,
                dataset_id,
                pipeline=workflow.repo_url,
                revision=workflow.default_revision,
                output_id=str(run_id),
                mode=tool_mode,
                form_data=payload.formData,
                user_email=user_email,
                full_name=full_name,
                institute=institute,
                ip_address=ip_address,
            )
        elif workflow_name == "interaction-screening":
            assert wisps_form_data is not None
            wisps_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            result = await launch_wisps_workflow(
                wisps_launch_form,
                dataset_id,
                pipeline=workflow.repo_url,
                revision=workflow.default_revision,
                config_path=workflow.config_path or "",
                form_data=wisps_form_data,
                output_id=str(run_id),
                user_email=user_email,
                full_name=full_name,
                institute=institute,
                ip_address=ip_address,
            )
        else:
            db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No executor configured for workflow '{workflow.name}'.",
            )
        workflow_run.seqera_run_id = result.workflow_id
        db_session.commit()
    except HTTPException:
        db_session.rollback()
        raise
    except (
        BindflowConfigurationError,
        ProteinfoldConfigurationError,
        WispsConfigurationError,
    ) as exc:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except (BindflowExecutorError, ProteinfoldExecutorError, WispsExecutorError) as exc:
        db_session.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update local workflow run after launch.",
        ) from exc

    return WorkflowLaunchResponse(
        message="Workflow launched successfully",
        runId=result.workflow_id,
        status=result.status,
        submitTime=submission_timestamp,
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


@router.post(
    "/datasets/upload",
    response_model=DatasetUploadResponse,
)
async def upload_dataset(
    payload: DatasetUploadRequest,
) -> DatasetUploadResponse:
    """Create a Seqera dataset and upload form data as CSV content."""
    try:
        dataset = await create_seqera_dataset(name="dataset")
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dataset creation failed: {exc}",
        ) from exc
    except SeqeraExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Dataset creation failed: {exc}",
        ) from exc

    try:
        upload_result = await upload_dataset_to_seqera(dataset.dataset_id, payload.formData)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dataset upload failed: {exc}",
        ) from exc
    except SeqeraExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Dataset upload failed: {exc}",
        ) from exc

    return DatasetUploadResponse(
        message="Dataset created and uploaded successfully",
        datasetId=upload_result.dataset_id,
        success=upload_result.success,
        details=upload_result.raw_response,
    )


@router.post(
    "/datasets/interaction-screening/upload",
    response_model=InteractionScreeningDatasetUploadResponse,
)
async def upload_interaction_screening_dataset_endpoint(
    payload: InteractionScreeningDatasetUploadRequest,
) -> InteractionScreeningDatasetUploadResponse:
    """Create a Seqera dataset and upload an interaction screening samplesheet."""
    try:
        dataset = await create_seqera_dataset(name=payload.runId)
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dataset creation failed: {exc}",
        ) from exc
    except SeqeraExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Dataset creation failed: {exc}",
        ) from exc

    try:
        upload_result = await upload_interaction_screening_dataset(
            dataset.dataset_id, payload.sequences, payload.runId
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dataset upload failed: {exc}",
        ) from exc
    except SeqeraExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Dataset upload failed: {exc}",
        ) from exc

    return InteractionScreeningDatasetUploadResponse(
        message="Dataset created and uploaded successfully",
        datasetId=upload_result.dataset_id,
        success=upload_result.success,
        splitOutputDir=upload_result.split_output_dir or "",
        details=upload_result.raw_response,
    )
