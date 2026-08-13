"""Proteinfold workflow executor for Seqera Platform (modeled after bindflow)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .launch_payloads import (
    DEFAULT_MODULE_LOADS,
    get_executor_script,
    inject_prerun_script,
    without_prerun_script,
)
from .proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_config_text,
    get_proteinfold_default_params,
)
from .seqera import (
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)

# Params forwarded from the frontend's Tool Settings (step 2)
_TOOL_PARAM_KEYS = frozenset(
    {
        "random_seed",
        "alphafold2_full_dbs",
        "colabfold_num_recycles",
        "colabfold_use_templates",
        "boltz_use_potentials",
    }
)


def _aws_prerun_env(settings: Settings) -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": settings.aws.access_key_id,
        "AWS_SECRET_ACCESS_KEY": settings.aws.secret_access_key,
        "AWS_REGION": settings.aws.region,
    }


def _tool_params(form_data: WorkflowFormData) -> dict[str, Any]:
    extra = form_data.extra_fields
    return {key: extra[key] for key in _TOOL_PARAM_KEYS if key in extra and extra[key] is not None}


def _build_params_text(
    out_dir: str,
    samplesheet_url: str,
    mode: str,
    form_data: WorkflowFormData | None,
    custom_params: str | None,
) -> str:
    """Build the YAML params string for the Seqera launch payload."""
    params = get_proteinfold_default_params(out_dir, samplesheet_url, mode)
    if form_data:
        params.update(_tool_params(form_data))
    params_text = params_to_yaml_text(params)
    if custom_params and custom_params.strip():
        params_text = f"{params_text}\n{custom_params.rstrip()}"
    return params_text


async def prepare_proteinfold_workflow(
    form: WorkflowLaunchForm,
    s3_input_key: str,
    *,
    settings: Settings,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str = "alphafold2",
    form_data: WorkflowFormData | None = None,
    user_details: WorkflowUserDetails,
    commit: bool = False,
) -> QueuedJob:
    """Build and queue a proteinfold launch payload."""
    workspace_id = settings.seqera.work_space
    compute_env_id = settings.seqera.compute_id
    work_dir = settings.seqera.work_dir
    s3_bucket = settings.aws.s3_bucket

    if not output_id or not output_id.strip():
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_id.strip()}"

    if not form.runName or not form.runName.strip():
        raise SeqeraConfigurationError("Missing run name for workflow launch")

    sheet_url = f"s3://{s3_bucket}/{s3_input_key}"
    params_text = _build_params_text(
        out_dir,
        sheet_url,
        mode,
        form_data,
        form.paramsText,
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
            user_details=user_details,
            gadi_project=settings.seqera.gadi_project,
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
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return queued_job


async def launch_proteinfold_workflow(
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a proteinfold workflow on the Seqera Platform."""
    settings = settings or get_settings()
    launch_payload = queued_job.launch_payload
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})
    logger.info(
        "Launching proteinfold workflow via Seqera API",
        extra={
            "workspaceId": launch_payload["workspaceId"],
            "computeEnvId": launch_payload["computeEnvId"],
            "pipeline": launch_payload["pipeline"],
            "runName": launch_payload["runName"],
        },
    )

    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
        module_loads=DEFAULT_MODULE_LOADS,
        env=_aws_prerun_env(settings),
    )
    runtime_payload = inject_prerun_script(
        launch_payload=launch_payload,
        prerun_script=prerun_script,
    )

    if dry_run:
        logger.info("Dry run - not launching proteinfold workflow")
        return None
    return await post_seqera_launch(
        {"launch": runtime_payload}, workflow_label="Proteinfold", settings=settings
    )
