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
    _samplesheet_url,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)


async def prepare_bindflow_workflow(  # pylint: disable=too-many-locals
    form: WorkflowLaunchForm,
    dataset_id: str,
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
) -> dict[str, Any]:
    """Build and queue a bindflow launch payload."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
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

    dataset_url = _samplesheet_url(seqera_api_url, workspace_id, dataset_id)
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
        "datasetIds": [dataset_id],
    }

    queued_job = QueuedJob(
        workflow=workflow_run.workflow,
        workflow_run=workflow_run,
        launch_payload=without_prerun_script(launch_payload),
        # TODO: set as submitted for now, we are still launching jobs immediately
        status="submitted",
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(queued_job)
    db_session.commit()
    return launch_payload


async def launch_bindflow_workflow(  # pylint: disable=too-many-locals
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    prerun_script_path: str | None = None,
    mode: str,
    form_data: WorkflowFormData,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
) -> WorkflowLaunchResult:
    """Launch a bindflow workflow on the Seqera Platform."""
    launch_payload = await prepare_bindflow_workflow(
        form,
        dataset_id,
        db_session=db_session,
        workflow_run=workflow_run,
        pipeline=pipeline,
        config_path=config_path,
        revision=revision,
        output_id=output_id,
        mode=mode,
        form_data=form_data,
        user_email=user_email,
        full_name=full_name,
        institute=institute,
        ip_address=ip_address,
    )

    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"

    # Log the complete params being sent
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})

    logger.info(
        "Launching bindflow workflow via Seqera API",
        extra={
            "url": url,
            "workspaceId": workspace_id,
            "computeEnvId": compute_env_id,
            "pipeline": launch_payload["pipeline"],
            "runName": launch_payload["runName"],
        },
    )

    prerun_script = get_executor_script(
        prerun_script_path=prerun_script_path,
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

    return await post_seqera_launch(url, {"launch": runtime_payload}, workflow_label="Bindflow")
