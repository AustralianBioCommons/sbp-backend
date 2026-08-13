"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .seqera import (
    WorkflowLaunchResult,
    _get_required_env,
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


async def prepare_wisps_workflow(
    form: WorkflowLaunchForm,
    *,
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

    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")
    s3_bucket = _get_required_env("AWS_S3_BUCKET")

    if not output_id or not output_id.strip():
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_id.strip()}"

    job_id = (form.runName or "").strip()
    if not job_id:
        raise SeqeraConfigurationError("Missing run name for workflow launch")

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
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch an interaction screening (WISPS) workflow on the Seqera Platform."""
    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
    )
    runtime_payload = inject_prerun_script(
        launch_payload=queued_job.launch_payload, prerun_script=prerun_script
    )

    if dry_run:
        logger.info("Dry run - not launching WISPS workflow")
        return None
    return await post_seqera_launch(payload={"launch": runtime_payload}, workflow_label="WISPS")
