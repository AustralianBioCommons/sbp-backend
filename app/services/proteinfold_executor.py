"""Proteinfold workflow executor for Seqera Platform (modeled after bindflow)."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows import WorkflowFormData, WorkflowLaunchForm
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_config_text,
    get_proteinfold_default_params,
)
from .seqera import (
    WorkflowLaunchResult,
    _get_required_env,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)

# Params forwarded from the frontend's Tool Settings (step 2)
_TOOL_PARAM_KEYS = frozenset(
    {
        "alphafold2_random_seed",
        "alphafold2_full_dbs",
        "colabfold_num_recycles",
        "colabfold_use_templates",
        "boltz_use_potentials",
    }
)


def _tool_params(form_data: WorkflowFormData) -> dict[str, Any]:
    extra = form_data.extra_fields
    return {key: extra[key] for key in _TOOL_PARAM_KEYS if key in extra and extra[key] is not None}


def _build_params_text(
    out_dir: str,
    samplesheet_url: str,
    mode: str,
    form_data: WorkflowFormData | None,
    custom_params: str | None,
    extra_params: dict[str, Any] | None = None,
) -> str:
    """Build the YAML params string for the Seqera launch payload."""
    params = get_proteinfold_default_params(out_dir, samplesheet_url, mode)
    if form_data:
        params.update(_tool_params(form_data))
    if extra_params:
        params.update(extra_params)
    params_text = params_to_yaml_text(params)
    if custom_params and custom_params.strip():
        params_text = f"{params_text}\n{custom_params.rstrip()}"
    return params_text


async def prepare_proteinfold_workflow(
    form: WorkflowLaunchForm,
    s3_input_key: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str = "alphafold2",
    form_data: WorkflowFormData | None = None,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
):
    """Build and queue a proteinfold launch payload."""
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")
    s3_bucket = _get_required_env("AWS_S3_BUCKET")

    if not output_id or not output_id.strip():
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_id.strip()}"

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    job_id = (form.runName or "").strip()
    if not job_id:
        raise SeqeraConfigurationError("Missing run name for workflow launch")

    sheet_url = f"s3://{s3_bucket}/{s3_input_key}"
    params_text = _build_params_text(
        out_dir,
        sheet_url,
        mode,
        form_data,
        form.paramsText,
        extra_params={"job_id": job_id, "user_name": user_email, "timestamp": timestamp},
    )

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": form.runName,
        "pipeline": pipeline,
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_proteinfold_config_profiles(),
        "configText": get_proteinfold_config_text(
            config_path,
            job_id=job_id,
            user_name=user_email,
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
        # TODO: set as submitted for now, we are still launching jobs immediately
        status="submitted",
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(queued_job)
    db_session.commit()
    return launch_payload


async def launch_proteinfold_workflow(
    form: WorkflowLaunchForm,
    s3_input_key: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    prerun_script_path: str | None = None,
    mode: str = "alphafold2",
    form_data: WorkflowFormData | None = None,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
) -> WorkflowLaunchResult:
    """Launch a proteinfold workflow on the Seqera Platform."""
    launch_payload = await prepare_proteinfold_workflow(
        form,
        s3_input_key,
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
    launch_url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})
    logger.info(
        "Launching proteinfold workflow via Seqera API",
        extra={
            "url": launch_url,
            "workspaceId": workspace_id,
            "computeEnvId": compute_env_id,
            "pipeline": pipeline,
            "runName": form.runName,
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
        launch_payload=launch_payload,
        prerun_script=prerun_script,
    )

    return await post_seqera_launch(
        launch_url, {"launch": runtime_payload}, workflow_label="Proteinfold"
    )
