"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
import os
import shlex
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .globus_transfer import build_gadi_input_path, build_gadi_output_path
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .results_utils import s3_uri_to_key
from .seqera import (
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import WorkflowLaunchError
from .wisps_config import (
    WISPS_WORKFLOW_MODES,
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
)

logger = logging.getLogger(__name__)


async def prepare_wisps_workflow(
    form: WorkflowLaunchForm,
    *,
    settings: Settings,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    form_data: WorkflowFormData,
    revision: str | None = None,
    output_id: str | None = None,
    user_details: WorkflowUserDetails,
    staged_input_location: str,
    commit: bool = False,
) -> QueuedJob:
    tool: str | None = form_data.tool or None

    workspace_id = settings.seqera.work_space
    compute_env_id = settings.seqera.compute_id
    work_dir = settings.seqera.work_dir

    if not output_id or not output_id.strip():
        raise WorkflowLaunchError("Missing output identifier for workflow launch")
    out_dir = build_gadi_output_path(
        output_id.strip(),
        form_data.workflow,
        globus_settings=settings.globus,
    )

    job_id = (form.runName or "").strip()
    if not job_id:
        raise WorkflowLaunchError("Missing run name for workflow launch")

    mode = WISPS_WORKFLOW_MODES.get(form_data.workflow, "g1-g2")
    sheet_url = staged_input_location
    params_text = params_to_yaml_text(
        get_wisps_default_params(
            out_dir=out_dir,
            samplesheet_url=sheet_url,
            mode=mode,
            tool=tool,
        )
    )

    config_text = get_wisps_config_text(
        config_path,
        user_details=user_details,
        gadi_project=settings.seqera.gadi_project,
    )

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": form.runName,
        "pipeline": pipeline,
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "main",
        "paramsText": params_text,
        "configProfiles": get_wisps_config_profiles(),
        "configText": config_text,
        "resume": False,
    }

    queued_job = QueuedJob(
        workflow=workflow_run.workflow,
        workflow_run=workflow_run,
        launch_payload=without_prerun_script(launch_payload),
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(queued_job)
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return queued_job


async def launch_wisps_workflow(
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch an interaction screening (WISPS) workflow on the Seqera Platform."""
    settings = settings or get_settings()
    if not queued_job.workflow_run.submitted_form_data:
        raise ValueError("No submitted form data found for queued job")
    form_data = WorkflowFormData.model_validate(queued_job.workflow_run.submitted_form_data)
    fasta_uri = (form_data.extra_fields.get("fastaS3Uri") or "").strip()
    split_output_dir = (form_data.extra_fields.get("splitOutputDir") or "").strip()
    if not fasta_uri or not split_output_dir:
        raise ValueError("Missing fastaS3Uri/splitOutputDir in submitted form data")

    fasta_key = s3_uri_to_key(fasta_uri)
    if not fasta_key:
        raise ValueError(f"Invalid S3 URI for fastaS3Uri: {fasta_uri}")
    if queued_job.workflow_run.workflow is None:
        raise ValueError("Queued job's workflow run has no associated workflow")
    # Matches the destination_location computed by _stage_wisps_fasta at queue
    # time (app/routes/workflows.py) - the aggregated FASTA Globus stages to.
    staged_fasta_location = build_gadi_input_path(
        queued_job.workflow_run.id,
        queued_job.workflow_run.workflow.name.lower(),
        os.path.basename(fasta_key),
        globus_settings=settings.globus,
    )

    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
        repo_gadi_path=queued_job.workflow.repo_gadi_path,
    )
    # wisps_prerun.sh splits the staged aggregated FASTA into the per-sequence
    # files the samplesheet references, reading F (input) and D (output dir) as
    # shell vars - get_executor_script no longer injects per-run env, so prepend
    # them here instead.
    prerun_script = (
        f"F={shlex.quote(staged_fasta_location)}\n"
        f"D={shlex.quote(split_output_dir)}\n" + prerun_script
    )
    runtime_payload = inject_prerun_script(
        launch_payload=queued_job.launch_payload, prerun_script=prerun_script
    )

    if dry_run:
        logger.info("Dry run - not launching WISPS workflow")
        return None
    return await post_seqera_launch(
        payload={"launch": runtime_payload}, workflow_label="WISPS", settings=settings
    )
