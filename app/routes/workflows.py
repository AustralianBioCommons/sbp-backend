"""Workflow-related HTTP routes."""

from __future__ import annotations

import logging
import random
import re
import string
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session
from unidecode import unidecode

from ..db.models import QueuedJob
from ..db.models.core import AppUser, RunInput, RunMetric, S3Object, Workflow, WorkflowRun
from ..schemas.workflows import (
    DatasetUploadRequest,
    InteractionScreeningDatasetUploadRequest,
    InteractionScreeningFormData,
    InteractionScreeningS3UploadResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    RunInputPresignedUrlResponse,
    S3DatasetUploadResponse,
    WorkflowFormData,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
    WorkflowUserDetails,
)
from ..services.bindflow_executor import _get_required_env, prepare_bindflow_workflow
from ..services.credits import (
    WorkflowCreditsResponse,
    compute_cost,
    is_credits_enabled,
    list_workflow_credit_configs,
)
from ..services.datasets import (
    upload_csv_to_s3,
    upload_interaction_screening_csv_to_s3,
)
from ..services.proteinfold_executor import prepare_proteinfold_workflow
from ..services.s3 import S3ConfigurationError, S3ServiceError, generate_presigned_url
from ..services.seqera_errors import SeqeraConfigurationError
from ..services.wisps_executor import prepare_wisps_workflow
from .dependencies import (
    get_client_ip,
    get_current_user_id,
    get_db,
    require_workflow_execution_role,
)

logger = logging.getLogger(__name__)

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
    now = datetime.now(UTC)
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
    except TypeError, ValueError:
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

    The frontend computes a run's display cost locally from these multipliers;
    the backend remains the single source of truth for the authoritative
    deduction at launch (see ``launch_workflow``).
    """
    return WorkflowCreditsResponse(workflows=list(list_workflow_credit_configs()))


def _launch_credit_cost(category: str, tool: str, final_design_count: int | None) -> int | None:
    """Authoritative per-run cost for workflows charged server-side at launch.

    Only de-novo (final designs) and single (constant) are charged today — their
    quantity is fully determined by the launch payload. interaction/bulk are not
    charged here (display-only); they return None.
    """
    cat = category.strip().lower()
    if cat == "single-prediction":
        return compute_cost(cat, tool, 1)
    if cat == "de-novo-design":
        if final_design_count is None or final_design_count < 1:
            return None
        return compute_cost(cat, tool, final_design_count)
    return None


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

    s3_input_key = payload.s3InputKey.strip()
    if not s3_input_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="s3InputKey is required and must not be empty.",
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
    # removes everything that isn't a letter, digit, or space
    name = unidecode(user.name or "")
    full_name = re.sub(r"[^a-zA-Z0-9 ]", "", name).replace(" ", "_")
    institute = user_email.split("@")[-1] if "@" in user_email else None
    ip_address: str | None = launch_ip or None

    full_name = _require_launch_var("full_name", full_name)
    institute = _require_launch_var("institute", institute)
    ip_address = _require_launch_var("ip_address", ip_address)
    user_details = WorkflowUserDetails(
        user_email=user_email,
        full_name=full_name,
        institute=institute,
        ip_address=ip_address,
    )

    # Authoritative credit cost (server-side, non-spoofable). Only charged for
    # workflows whose quantity is fully determined by the launch payload
    # (de-novo, single); interaction/bulk are display-only for now. Gated by the
    # ENABLE_CREDITS flag so the feature can be rolled out independently.
    run_credit_cost = (
        _launch_credit_cost(requested_workflow, selected_tool, final_design_count)
        if is_credits_enabled()
        else None
    )
    if run_credit_cost is not None:
        balance = db_session.scalar(select(AppUser.credit).where(AppUser.id == current_user_id))
        if balance is None or balance < run_credit_cost:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient credits to launch this workflow.",
            )

    run_id = uuid4()
    run_work_dir = f"{_get_required_env('WORK_DIR').rstrip('/')}/{run_id}"
    submission_timestamp = datetime.now(UTC)

    # Reserve DB row first so a queued workflow always has a DB entry.
    # Use local run UUID as a temporary seqera_run_id placeholder.
    workflow_run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        owner_user_id=current_user_id,
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

    s3_bucket = _get_required_env("AWS_S3_BUCKET")
    s3_input_uri = f"s3://{s3_bucket}/{s3_input_key}"
    if db_session.get(S3Object, s3_input_key) is None:
        db_session.add(S3Object(object_key=s3_input_key, uri=s3_input_uri))
    db_session.add(RunInput(run_id=run_id, s3_object_id=s3_input_key))
    db_session.commit()

    workflow_name = workflow.name.lower()

    # All workflows require config_path. Validate before the try block
    # so that HTTPException is not swallowed by the generic except Exception handler.
    if not workflow.config_path:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workflow '{workflow.name}' is missing config_path in workflows table.",
        )

    wisps_form_data: InteractionScreeningFormData | None = None
    if workflow_name == "interaction-screening":
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
        queued_job: QueuedJob
        seqera_run_name = build_unique_run_name(payload.launch.runName or "")
        if workflow_name in ("single-prediction", "proteinfold"):
            # single-prediction → proteinfold executor.
            # selected_tool carries the chosen algorithm ("colabfold", "alphafold2", "boltz").
            tool_algo = selected_tool
            proteinfold_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            queued_job = await prepare_proteinfold_workflow(
                proteinfold_launch_form,
                s3_input_key,
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline=workflow.repo_url,
                config_path=workflow.config_path,
                revision=workflow.default_revision,
                output_id=str(run_id),
                mode=tool_algo,
                form_data=payload.formData,
                user_details=user_details,
            )
        elif workflow_name in ("de-novo-design", "bindflow", "bindcraft"):
            # de-novo-design → bindflow executor.
            # selected_tool carries the chosen algorithm ("bindcraft", "rfdiffusion").
            tool_mode = selected_tool
            bindcraft_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            queued_job = await prepare_bindflow_workflow(
                bindcraft_launch_form,
                s3_input_key,
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline=workflow.repo_url,
                config_path=workflow.config_path,
                revision=workflow.default_revision,
                output_id=str(run_id),
                mode=tool_mode,
                form_data=payload.formData,
                user_details=user_details,
            )
        elif workflow_name == "interaction-screening":
            assert wisps_form_data is not None
            wisps_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            queued_job = await prepare_wisps_workflow(
                wisps_launch_form,
                s3_input_key,
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline=workflow.repo_url,
                revision=workflow.default_revision,
                config_path=workflow.config_path,
                form_data=wisps_form_data,
                output_id=str(run_id),
                user_details=user_details,
            )
        else:
            db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No executor configured for workflow '{workflow.name}'.",
            )

        # Deduct the run's credit cost now that the job is accepted into the queue. Atomic and
        # guarded (credit >= cost) so the balance can't go negative; committed
        # together with the queued run finalisation.
        if run_credit_cost is not None:
            deducted = cast(
                CursorResult,
                db_session.execute(
                    update(AppUser)
                    .where(
                        AppUser.id == current_user_id,
                        AppUser.credit >= run_credit_cost,
                    )
                    .values(
                        credit=AppUser.credit - run_credit_cost,
                        credit_updated_at=datetime.now(UTC),
                        credit_updated_by=user_email,
                    )
                ),
            )
            if deducted.rowcount == 0:
                logger.warning(
                    "Queued run %s but could not deduct %s credits from user %s "
                    "(balance changed since the pre-launch check)",
                    run_id,
                    run_credit_cost,
                    current_user_id,
                )
        db_session.commit()
    except HTTPException:
        db_session.rollback()
        raise
    except SeqeraConfigurationError as exc:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except Exception as exc:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue local workflow run.",
        ) from exc

    return WorkflowLaunchResponse(
        message="Workflow queued successfully",
        runId=str(run_id),
        status=queued_job.status,
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
    iso_now = datetime.now(UTC).isoformat()
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
    response_model=S3DatasetUploadResponse,
)
async def upload_dataset(
    payload: DatasetUploadRequest,
) -> S3DatasetUploadResponse:
    """Generate a CSV from form data and upload directly to S3."""
    try:
        result = await upload_csv_to_s3(payload.formData)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 upload failed: {exc}",
        ) from exc

    return S3DatasetUploadResponse(
        message="CSV samplesheet uploaded to S3 successfully",
        s3Key=result.file_key,
        s3Uri=result.file_url or f"s3://{result.bucket}/{result.file_key}",
        success=result.success,
    )


@router.post(
    "/datasets/interaction-screening/upload",
    response_model=InteractionScreeningS3UploadResponse,
)
async def upload_interaction_screening_dataset_endpoint(
    payload: InteractionScreeningDatasetUploadRequest,
) -> InteractionScreeningS3UploadResponse:
    """Build and upload an interaction screening samplesheet directly to S3."""
    try:
        result, split_output_dir = await upload_interaction_screening_csv_to_s3(
            payload.sequences, payload.runId
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 upload failed: {exc}",
        ) from exc

    return InteractionScreeningS3UploadResponse(
        message="Interaction screening samplesheet uploaded to S3 successfully",
        s3Key=result.file_key,
        s3Uri=result.file_url or f"s3://{result.bucket}/{result.file_key}",
        success=result.success,
        splitOutputDir=split_output_dir,
    )


@router.get(
    "/runs/{run_id}/input-samplesheet",
    response_model=RunInputPresignedUrlResponse,
)
async def get_run_input_samplesheet(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db_session: Session = Depends(get_db),
) -> RunInputPresignedUrlResponse:
    """Return a pre-signed URL to download the input samplesheet for a workflow run.

    Access is restricted to the owning user.
    """
    workflow_run = db_session.scalar(
        select(WorkflowRun).where(
            WorkflowRun.seqera_run_id == run_id,
            WorkflowRun.owner_user_id == current_user_id,
        )
    )
    if workflow_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow run not found or access denied.",
        )

    run_input = next(iter(workflow_run.inputs), None)
    if run_input is None or run_input.s3_object is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No input samplesheet found for this workflow run.",
        )

    s3_key = run_input.s3_object.object_key
    try:
        presigned_url = await generate_presigned_url(
            s3_key,
            expiration=3600,
            response_content_type="text/csv",
            response_content_disposition="attachment",
        )
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate download URL: {exc}",
        ) from exc

    return RunInputPresignedUrlResponse(
        runId=run_id,
        s3Key=s3_key,
        presignedUrl=presigned_url,
    )
