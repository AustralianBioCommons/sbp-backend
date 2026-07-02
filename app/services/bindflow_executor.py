"""Bindflow workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows import WorkflowFormData, WorkflowLaunchForm
from .bindflow_config import (
    get_bindflow_config_profiles,
    get_bindflow_config_text,
    get_bindflow_default_params,
)
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .seqera import (
    WorkflowLaunchResult,
    _get_required_env,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)


async def prepare_bindflow_workflow(  # pylint: disable=too-many-locals
    form: WorkflowLaunchForm,
    s3_input_key: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str,
    form_data: WorkflowFormData,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
) -> QueuedJob:
    """Build and queue a bindflow launch payload."""
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")
    s3_bucket = _get_required_env("AWS_S3_BUCKET")

    run_name = (form.runName or "").strip()
    if not run_name:
        raise SeqeraConfigurationError("Missing run name for workflow launch")
    # Always use a unique backend-generated ID for outputs to avoid S3 prefix collisions.
    output_key = (output_id or "").strip()
    if not output_key:
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_key}"

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    dataset_url = f"s3://{s3_bucket}/{s3_input_key}"
    default_params = get_bindflow_default_params(out_dir, dataset_url)

    default_params["job_id"] = run_name
    default_params["user_name"] = user_email
    default_params["timestamp"] = timestamp
    default_params["mode"] = mode

    # Merge any tool-specific params forwarded from the frontend form
    for key, value in form_data.extra_fields.items():
        if key not in default_params and value is not None:
            default_params[key] = value

    # Serialize to YAML
    params_text = params_to_yaml_text(default_params)

    # Add custom paramsText from frontend if provided
    if form.paramsText and form.paramsText.strip():
        params_text = f"{params_text}\n{form.paramsText.rstrip()}"

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": run_name,
        "pipeline": pipeline,
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_bindflow_config_profiles(),
        "configText": get_bindflow_config_text(
            config_path,
            job_id=run_name,
            username=user_email,
            timestamp=timestamp,
            full_name=full_name,
            institute=institute,
            ip_address=ip_address,
        ),
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
    db_session.commit()
    return queued_job


async def launch_bindflow_workflow(  # pylint: disable=too-many-locals
    *,
    queued_job: QueuedJob,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a bindflow workflow on the Seqera Platform."""
    launch_payload = queued_job.launch_payload

    # Log the complete params being sent
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})

    logger.info(
        "Launching bindflow workflow via Seqera API",
        extra={
            "workspaceId": launch_payload["workspaceId"],
            "computeEnvId": launch_payload["computeEnvId"],
            "pipeline": launch_payload["pipeline"],
            "runName": launch_payload["runName"],
        },
    )

    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
        module_loads=["singularity", "nextflow"],
        env={
            "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", ""),
            "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            "AWS_REGION": os.getenv("AWS_REGION", "ap-southeast-2"),
        },
    )
    runtime_payload = inject_prerun_script(
        launch_payload=launch_payload, prerun_script=prerun_script
    )

    if dry_run:
        logger.info("Dry run - not launching bindflow workflow")
        return None
    return await post_seqera_launch({"launch": runtime_payload}, workflow_label="Bindflow")
