"""Workflow-related HTTP routes."""

from __future__ import annotations

import logging
import os
import random
import re
import string
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.orm import Session

from ..config import GlobusSettings, Settings, get_settings
from ..db.models import QueuedJob
from ..db.models.core import (
    AppUser,
    DataTransfer,
    RunInput,
    RunMetric,
    S3Object,
    Workflow,
    WorkflowRun,
)
from ..schemas.workflows.interaction_screening import WispsDatasetUploadRequest, WispsFormData
from ..schemas.workflows.shared import (
    DatasetUploadRequest,
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
from ..schemas.workflows.single_prediction import (
    SinglePredictionEntity,
    validate_single_prediction_entities,
)
from ..services.bindflow_executor import prepare_bindflow_workflow, resolve_bindflow_asset_path
from ..services.credits import (
    WorkflowCreditsResponse,
    is_credits_enabled,
    launch_credit_cost,
    list_workflow_credit_configs,
)
from ..services.datasets import (
    BULK_PREDICTION_BASE_PATH,
    INTERACTION_SCREENING_BASE_PATH,
    upload_csv_to_s3,
    upload_wisps_samplesheet_to_s3,
)
from ..services.globus_transfer import build_gadi_input_path
from ..services.proteindj_executor import prepare_proteindj_workflow
from ..services.proteinfold_executor import prepare_proteinfold_workflow
from ..services.results_utils import s3_uri_to_key
from ..services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    read_csv_from_s3,
)
from ..services.seqera_errors import WorkflowLaunchError
from ..services.wisps_executor import prepare_wisps_workflow
from ..services.workflow_repo_staging import RepoStagingError, ensure_repo_staging_requested
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
    """Credit-cost quantity for a launch. Sourced from max_trajectories (the
    "Number of Trajectories" form field) rather than number_of_final_designs,
    since the latter is no longer user-facing for bindcraft — it's derived
    server-side when the bindflow samplesheet is built (see
    datasets.upload_csv_to_s3) and isn't present in the launch payload.
    """
    if not isinstance(form_data, WorkflowFormData):
        return None
    value = form_data.extra_fields.get("max_trajectories")
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except TypeError, ValueError:
        return None
    return parsed if parsed >= 1 else None


def _validate_single_prediction_form(form_data: WorkflowFormData, tool: str) -> None:
    """Validate single-prediction entity limits; raise HTTPException on failure.

    Guards against jobs with too many entities or has no protein input
    """
    raw_entities = form_data.extra_fields.get("entities")
    if not isinstance(raw_entities, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'entities' is required in formData for single-prediction.",
        )
    try:
        entities = [SinglePredictionEntity.model_validate(item) for item in raw_entities]
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid entity data in formData for single-prediction.",
        ) from exc

    raw_potentials = form_data.extra_fields.get("boltz_use_potentials")
    boltz_use_potentials = (
        raw_potentials.strip().lower() in ("true", "1", "yes")
        if isinstance(raw_potentials, str)
        else bool(raw_potentials)
    )

    try:
        validate_single_prediction_entities(
            entities, tool, boltz_use_potentials=boltz_use_potentials
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


async def _stage_referenced_samplesheet_file(
    *,
    db_session: Session,
    s3_input_key: str,
    field_name: str,
    run_id: UUID,
    workflow_name: str,
    globus_settings: GlobusSettings,
) -> str:
    """Stage a file referenced by a samplesheet column to Gadi via Globus, and
    return the s3InputKey of a corrected samplesheet with that column rewritten
    to the local path.

    Some samplesheets (bindcraft's starting_pdb, proteinfold's fasta) carry a raw
    S3 URI to a separately-uploaded file. Globus stages the samplesheet CSV to
    Gadi as-is, but the pipeline reads that column as a local file path, not an
    S3 URI (unlike ProteinDJ, which takes its pdb path as a direct pipeline
    param, never via a samplesheet) - so the referenced file must be staged
    separately and the samplesheet corrected to point at where it lands.
    """
    try:
        samplesheet_rows = await read_csv_from_s3(s3_input_key)
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to read samplesheet at s3InputKey: {exc}",
        ) from exc
    if not samplesheet_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Samplesheet at s3InputKey is empty.",
        )
    samplesheet_row = samplesheet_rows[0]
    source_uri = (samplesheet_row.get(field_name) or "").strip()
    if not source_uri:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{field_name}' is required in the samplesheet for {workflow_name}.",
        )
    source_key = s3_uri_to_key(source_uri)
    if not source_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid S3 URI for {field_name}.",
        )
    if db_session.get(S3Object, source_key) is None:
        db_session.add(S3Object(object_key=source_key, uri=source_uri))
    staged_location = build_gadi_input_path(
        run_id,
        workflow_name,
        os.path.basename(source_key),
        globus_settings=globus_settings,
    )
    db_session.add(
        RunInput(
            run_id=run_id,
            s3_object_id=source_key,
            data_transfer=DataTransfer(
                workflow_run_id=run_id,
                direction="input",
                provider="globus",
                source_location=source_uri,
                destination_location=staged_location,
                recursive=False,
            ),
        )
    )
    samplesheet_row[field_name] = staged_location
    try:
        csv_upload = await upload_csv_to_s3(samplesheet_row)
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to re-upload corrected samplesheet: {exc}",
        ) from exc
    return csv_upload.file_key


async def _rewrite_bindflow_settings_asset_columns(
    *, s3_input_key: str, repo_assets_path: str
) -> str:
    """Fill in settings_filters/settings_advanced samplesheet columns with the
    local Gadi path to bindflow's bundled default JSON files - the frontend
    leaves these fields unset (see sbp-portal's de-novo-design.ts), so
    resolve_bindflow_asset_path fills in the known default; any other,
    genuinely custom value is left untouched.

    Unlike starting_pdb (_stage_referenced_samplesheet_file, above) these
    columns don't need their own Globus transfer or RunInput/DataTransfer
    bookkeeping - they reference files that are already part of the workflow
    repo, staged as a whole. This is a plain string rewrite, re-uploaded only
    if something actually changed.
    """
    try:
        samplesheet_rows = await read_csv_from_s3(s3_input_key)
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to read samplesheet at s3InputKey: {exc}",
        ) from exc
    if not samplesheet_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Samplesheet at s3InputKey is empty.",
        )
    samplesheet_row = samplesheet_rows[0]
    changed = False
    for field_name in ("settings_filters", "settings_advanced"):
        resolved = resolve_bindflow_asset_path(
            field_name, samplesheet_row.get(field_name), repo_assets_path=repo_assets_path
        )
        if resolved is not None and resolved != samplesheet_row.get(field_name):
            samplesheet_row[field_name] = resolved
            changed = True
    if not changed:
        return s3_input_key
    try:
        csv_upload = await upload_csv_to_s3(samplesheet_row)
    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {exc}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to re-upload corrected samplesheet: {exc}",
        ) from exc
    return csv_upload.file_key


def _stage_wisps_fasta(
    *,
    db_session: Session,
    fasta_uri: str,
    run_id: UUID,
    workflow_name: str,
    globus_settings: GlobusSettings,
) -> None:
    """Stage the aggregated multi-sequence FASTA (formData.fastaS3Uri) to Gadi via
    Globus.

    Unlike bindcraft/proteinfold, WISPS's samplesheet never references this file
    directly - each row instead references a future per-sequence split file
    under formData.splitOutputDir, produced by the prerun script. So there's no
    samplesheet column to rewrite here, just the raw file to stage so that
    split step has something local to read.
    """
    fasta_key = s3_uri_to_key(fasta_uri)
    if not fasta_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid S3 URI for fastaS3Uri.",
        )
    if db_session.get(S3Object, fasta_key) is None:
        db_session.add(S3Object(object_key=fasta_key, uri=fasta_uri))
    db_session.add(
        RunInput(
            run_id=run_id,
            s3_object_id=fasta_key,
            data_transfer=DataTransfer(
                workflow_run_id=run_id,
                direction="input",
                provider="globus",
                source_location=fasta_uri,
                destination_location=build_gadi_input_path(
                    run_id,
                    workflow_name,
                    os.path.basename(fasta_key),
                    globus_settings=globus_settings,
                ),
                recursive=False,
            ),
        )
    )


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
    settings: Settings = Depends(get_settings),
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
    # ("single-prediction", "de-novo-design", etc.). A workflow's tool column is
    # NULL for a single row shared by all of its tools (e.g. single-prediction),
    # or set on multiple rows when each tool needs its own repo_url/config_path/
    # default_revision (e.g. de-novo-design: bindcraft vs rfdiffusion). An exact
    # tool match is preferred over the generic NULL-tool row when both exist.
    tool_matches = func.lower(Workflow.tool) == selected_tool.lower()
    workflow = db_session.scalar(
        select(Workflow)
        .where(
            func.lower(Workflow.name) == requested_workflow,
            or_(Workflow.tool.is_(None), tool_matches),
        )
        .order_by(tool_matches.desc())
        .limit(1)
    )
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Workflow '{payload.launch.workflow}' with tool '{selected_tool}' is not "
                "configured in workflows table. Seed the workflows catalog before launching."
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

    # Gadi compute nodes have no network access, so Nextflow can't fetch the
    # pipeline from GitHub itself - it must already be staged there
    try:
        repo_staging_locations = ensure_repo_staging_requested(
            db_session, workflow, settings=settings
        )
    except RepoStagingError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to resolve workflow repo: {exc}",
        ) from exc
    # Seqera's launch API validates `pipeline` as a URL, Nextflow's local-path scheme is "file:" with a
    # single slash before the (already-absolute) path
    repo_gadi_path = repo_staging_locations.gadi_path
    pipeline_url = f"file:{repo_gadi_path}"
    # bindcraft's settings_filters/settings_advanced reference files bundled
    # inside the workflow repo itself - repo_gadi_path is a bare git repo
    # (see build_repo_gadi_path) with no working-tree files on disk, so asset
    # resolution uses the separate plain checkout staged alongside it instead
    # (see build_repo_assets_gadi_path, _rewrite_bindflow_settings_asset_columns).
    repo_assets_path = repo_staging_locations.assets_gadi_path

    user = db_session.execute(
        select(AppUser.email).where(AppUser.id == current_user_id)
    ).one_or_none()
    if not user or not user.email:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve user details required for workflow launch.",
        )
    user_details = WorkflowUserDetails(
        user_email=user.email,
        ip_address=_require_launch_var("ip_address", launch_ip or None),
    )

    # Authoritative credit cost (server-side, non-spoofable). Only charged for
    # workflows whose quantity is fully determined by the launch payload
    # (de-novo, single); interaction/bulk are display-only for now. Gated by the
    # ENABLE_CREDITS flag so the feature can be rolled out independently.
    run_credit_cost = (
        launch_credit_cost(requested_workflow, selected_tool, final_design_count)
        if is_credits_enabled(settings)
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
    workflow_name = workflow.name.lower()
    run_work_dir = f"{settings.seqera.work_dir}/{run_id}"
    submission_timestamp = datetime.now(UTC)

    # Reserve DB row first so a queued workflow always has a DB entry.
    workflow_run = WorkflowRun(
        id=run_id,
        workflow=workflow,
        owner_user_id=current_user_id,
        seqera_run_id=None,
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

    # All workflows require config_path. Validate before the try block
    # so that HTTPException is not swallowed by the generic except Exception handler.
    if not workflow.config_path:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workflow '{workflow.name}' is missing config_path in workflows table.",
        )

    wisps_form_data: WispsFormData | None = None
    if workflow_name in ("interaction-screening", "bulk-prediction"):
        try:
            wisps_form_data = WispsFormData.model_validate(payload.formData.model_dump())
        except ValidationError as exc:
            missing = next(
                (str(e["loc"][-1]) for e in exc.errors() if e.get("loc")),
                "formData",
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{missing}' is required in formData for {workflow_name}.",
            ) from exc

    if workflow_name in ("single-prediction", "proteinfold"):
        _validate_single_prediction_form(payload.formData, selected_tool)

    # Validation above must run before any of this, since it involves real S3/Globus
    # I/O that a malformed request shouldn't pay the cost of (and shouldn't be able
    # to trigger before its own formData is validated).
    is_rfdiffusion_launch = (
        workflow_name in ("de-novo-design", "bindflow", "bindcraft")
        and selected_tool.lower() == "rfdiffusion"
    )
    is_bindcraft_launch = (
        workflow_name in ("de-novo-design", "bindflow", "bindcraft") and not is_rfdiffusion_launch
    )
    is_proteinfold_launch = workflow_name in ("single-prediction", "proteinfold")
    is_wisps_launch = workflow_name in ("interaction-screening", "bulk-prediction")
    if is_bindcraft_launch:
        s3_input_key = await _stage_referenced_samplesheet_file(
            db_session=db_session,
            s3_input_key=s3_input_key,
            field_name="starting_pdb",
            run_id=run_id,
            workflow_name=workflow_name,
            globus_settings=settings.globus,
        )
        s3_input_key = await _rewrite_bindflow_settings_asset_columns(
            s3_input_key=s3_input_key, repo_assets_path=repo_assets_path
        )
    elif is_proteinfold_launch:
        s3_input_key = await _stage_referenced_samplesheet_file(
            db_session=db_session,
            s3_input_key=s3_input_key,
            field_name="fasta",
            run_id=run_id,
            workflow_name=workflow_name,
            globus_settings=settings.globus,
        )
    elif is_wisps_launch:
        assert wisps_form_data is not None
        _stage_wisps_fasta(
            db_session=db_session,
            fasta_uri=wisps_form_data.fastaS3Uri,
            run_id=run_id,
            workflow_name=workflow_name,
            globus_settings=settings.globus,
        )

    staged_input_location: str | None = None
    if not is_rfdiffusion_launch:
        # rfdiffusion (ProteinDJ) has no samplesheet: s3InputKey for it is the
        # starting PDB's own S3 URI, not a bare key (see de-novo-design.ts, which
        # skips the samplesheet upload for this tool and reuses starting_pdb's URI
        # directly). prepare_proteindj_workflow stages that PDB itself via its own
        # DataTransfer, so staging "s3InputKey" here too would both double-prefix
        # the URI (it's already a full s3:// URI, not a bare key) and create a
        # second, unused DataTransfer for the same file.
        s3_bucket = settings.aws.s3_bucket
        s3_input_uri = f"s3://{s3_bucket}/{s3_input_key}"
        if db_session.get(S3Object, s3_input_key) is None:
            db_session.add(S3Object(object_key=s3_input_key, uri=s3_input_uri))
        staged_input_location = build_gadi_input_path(
            run_id,
            workflow_name,
            os.path.basename(s3_input_key),
            globus_settings=settings.globus,
        )
        input_transfer = DataTransfer(
            workflow_run_id=run_id,
            direction="input",
            provider="globus",
            source_location=s3_input_uri,
            destination_location=staged_input_location,
            recursive=False,
        )
        db_session.add(input_transfer)
        db_session.add(
            RunInput(run_id=run_id, s3_object_id=s3_input_key, data_transfer=input_transfer)
        )
        db_session.flush()

    try:
        queued_job: QueuedJob
        seqera_run_name = build_unique_run_name(payload.launch.runName or "")
        if workflow_name in ("single-prediction", "proteinfold"):
            # single-prediction → proteinfold executor.
            # selected_tool carries the chosen algorithm ("colabfold", "alphafold2", "boltz").
            tool_algo = selected_tool
            assert staged_input_location is not None
            proteinfold_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            queued_job = await prepare_proteinfold_workflow(
                proteinfold_launch_form,
                settings=settings,
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline=pipeline_url,
                config_path=workflow.config_path,
                revision=workflow.default_revision,
                output_id=str(run_id),
                mode=tool_algo,
                form_data=payload.formData,
                user_details=user_details,
                staged_input_location=staged_input_location,
            )
        elif workflow_name in ("de-novo-design", "bindflow", "bindcraft"):
            # de-novo-design → bindflow executor (bindcraft) or proteindj executor
            # (rfdiffusion), depending on the chosen algorithm.
            tool_mode = selected_tool
            de_novo_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            if tool_mode.lower() == "rfdiffusion":
                queued_job = await prepare_proteindj_workflow(
                    de_novo_launch_form,
                    settings=settings,
                    db_session=db_session,
                    workflow_run=workflow_run,
                    pipeline=pipeline_url,
                    config_path=workflow.config_path,
                    revision=workflow.default_revision,
                    output_id=str(run_id),
                    form_data=payload.formData,
                    user_details=user_details,
                )
            else:
                assert staged_input_location is not None
                queued_job = await prepare_bindflow_workflow(
                    de_novo_launch_form,
                    settings=settings,
                    db_session=db_session,
                    workflow_run=workflow_run,
                    pipeline=pipeline_url,
                    config_path=workflow.config_path,
                    revision=workflow.default_revision,
                    output_id=str(run_id),
                    form_data=payload.formData,
                    user_details=user_details,
                    staged_input_location=staged_input_location,
                    repo_assets_path=repo_assets_path,
                )
        elif workflow_name in ("interaction-screening", "bulk-prediction"):
            assert wisps_form_data is not None
            assert staged_input_location is not None
            wisps_launch_form = payload.launch.model_copy(update={"runName": seqera_run_name})
            queued_job = await prepare_wisps_workflow(
                wisps_launch_form,
                settings=settings,
                db_session=db_session,
                workflow_run=workflow_run,
                pipeline=pipeline_url,
                revision=workflow.default_revision,
                config_path=workflow.config_path,
                form_data=wisps_form_data,
                output_id=str(run_id),
                user_details=user_details,
                staged_input_location=staged_input_location,
            )
        else:
            db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No executor configured for workflow '{workflow.name}'.",
            )

        # Hold the job out of submit_pending_jobs until Globus data staging
        # completes (app/services/globus_transfer.py flips this to "pending").
        queued_job.status = "staging"
        db_session.add(queued_job)

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
                        credit_updated_by=user_details.user_email,
                    )
                ),
            )
            if deducted.rowcount == 0:
                logger.warning(
                    "Could not queue run %s because %s credits could not be deducted from user %s "
                    "(balance changed since the pre-launch check)",
                    run_id,
                    run_credit_cost,
                    current_user_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to launch this workflow.",
                )
        db_session.commit()
    except HTTPException:
        db_session.rollback()
        raise
    except WorkflowLaunchError as exc:
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
    settings: Settings = Depends(get_settings),
) -> S3DatasetUploadResponse:
    """Generate a CSV from form data and upload directly to S3."""
    try:
        result = await upload_csv_to_s3(
            payload.formData, settings=settings, workflow=payload.workflow
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

    return S3DatasetUploadResponse(
        message="CSV samplesheet uploaded to S3 successfully",
        s3Key=result.file_key,
        s3Uri=result.file_url or f"s3://{result.bucket}/{result.file_key}",
        success=result.success,
    )


_WISPS_BASE_PATHS: dict[str, str] = {
    "interaction-screening": INTERACTION_SCREENING_BASE_PATH,
    "bulk-prediction": BULK_PREDICTION_BASE_PATH,
}


@router.post(
    "/datasets/{workflow_name}/upload",
    response_model=S3DatasetUploadResponse,
)
async def upload_wisps_dataset_endpoint(
    workflow_name: Literal["interaction-screening", "bulk-prediction"],
    payload: WispsDatasetUploadRequest,
    settings: Settings = Depends(get_settings),
) -> S3DatasetUploadResponse:
    """Build and upload a WISPS samplesheet directly to S3."""
    base_path = _WISPS_BASE_PATHS[workflow_name]
    try:
        result, split_output_dir = await upload_wisps_samplesheet_to_s3(
            payload.sequences,
            payload.runId,
            base_path,
            workflow_name,
            include_group=workflow_name == "interaction-screening",
            settings=settings,
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

    return S3DatasetUploadResponse(
        message=f"{workflow_name} samplesheet uploaded to S3 successfully",
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
    settings: Settings = Depends(get_settings),
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
            settings=settings,
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
