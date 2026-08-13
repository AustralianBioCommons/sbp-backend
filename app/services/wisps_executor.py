"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .seqera import (
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError
from .wisps_config import (
    WISPS_WORKFLOW_MODES,
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
)

logger = logging.getLogger(__name__)


def _aws_prerun_env(settings: Settings) -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": settings.aws.access_key_id,
        "AWS_SECRET_ACCESS_KEY": settings.aws.secret_access_key,
        "AWS_REGION": settings.aws.region,
    }


async def prepare_wisps_workflow(
    form: WorkflowLaunchForm,
    s3_input_key: str,
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
    commit: bool = False,
) -> QueuedJob:
    tool: str | None = form_data.tool or None

    workspace_id = settings.seqera.work_space
    compute_env_id = settings.seqera.compute_id
    work_dir = settings.seqera.work_dir
    s3_bucket = settings.aws.s3_bucket

    if not output_id or not output_id.strip():
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_id.strip()}"

    job_id = (form.runName or "").strip()
    if not job_id:
        raise SeqeraConfigurationError("Missing run name for workflow launch")

    mode = WISPS_WORKFLOW_MODES.get(form_data.workflow, "g1-g2")
    sheet_url = f"s3://{s3_bucket}/{s3_input_key}"
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

    fasta_s3_uri = form_data.extra_fields.get("fastaS3Uri", "").strip()
    split_output_dir = form_data.extra_fields.get("splitOutputDir", "").strip()
    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
        env={
            **_aws_prerun_env(settings),
            "S3_PATH": fasta_s3_uri.replace("s3://", "", 1),
            "D": split_output_dir,
        },
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
