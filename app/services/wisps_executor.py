"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows import WorkflowFormData, WorkflowLaunchForm
from .launch_payloads import get_executor_script, inject_prerun_script, without_prerun_script
from .seqera import (
    WorkflowLaunchResult,
    _get_required_env,
    _samplesheet_url,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import SeqeraConfigurationError
from .wisps_config import (
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
)

logger = logging.getLogger(__name__)


async def prepare_wisps_workflow(
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    form_data: WorkflowFormData,
    revision: str | None = None,
    output_id: str | None = None,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
):
    tool: str | None = form_data.tool or None

    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")

    if not output_id or not output_id.strip():
        raise SeqeraConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{_get_required_env('AWS_S3_BUCKET')}/{output_id.strip()}"

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    job_id = (form.runName or "").strip()
    if not job_id:
        raise SeqeraConfigurationError("Missing run name for workflow launch")

    sheet_url = _samplesheet_url(seqera_api_url, workspace_id, dataset_id)
    params_text = params_to_yaml_text(
        get_wisps_default_params(out_dir=out_dir, samplesheet_url=sheet_url, tool=tool)
    )

    config_text = get_wisps_config_text(
        config_path,
        job_id=job_id,
        username=user_email,
        timestamp=timestamp,
        full_name=full_name,
        institute=institute,
        ip_address=ip_address,
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


async def launch_wisps_workflow(
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    form_data: WorkflowFormData,
    revision: str | None = None,
    output_id: str | None = None,
    prerun_script_path: str | None = None,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
) -> WorkflowLaunchResult:
    """Launch an interaction screening (WISPS) workflow on the Seqera Platform."""
    launch_payload = await prepare_wisps_workflow(
        form,
        dataset_id,
        db_session=db_session,
        workflow_run=workflow_run,
        pipeline=pipeline,
        config_path=config_path,
        form_data=form_data,
        revision=revision,
        output_id=output_id,
        user_email=user_email,
        full_name=full_name,
        institute=institute,
        ip_address=ip_address,
    )

    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    launch_url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"
    logger.info("WISPS launch paramsText", extra={"paramsText": launch_payload["paramsText"]})
    logger.info(
        "Launching WISPS workflow via Seqera API",
        extra={
            "url": launch_url,
            "workspaceId": workspace_id,
            "computeEnvId": compute_env_id,
            "pipeline": pipeline,
            "runName": form.runName,
        },
    )

    fasta_s3_uri = str(
        getattr(form_data, "fastaS3Uri", None) or form_data.extra_fields.get("fastaS3Uri") or ""
    ).strip()
    split_output_dir = str(
        getattr(form_data, "splitOutputDir", None)
        or form_data.extra_fields.get("splitOutputDir")
        or ""
    ).strip()
    runtime_payload = inject_prerun_script(
        launch_payload,
        prerun_script_path=prerun_script_path,
        build_script=lambda path: get_executor_script(
            prerun_script_path=path,
            env={
                "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", ""),
                "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                "AWS_REGION": os.getenv("AWS_REGION", "ap-southeast-2"),
                "S3_PATH": fasta_s3_uri.replace("s3://", "", 1),
                "D": split_output_dir,
            },
        ),
    )

    return await post_seqera_launch(launch_url, {"launch": runtime_payload}, workflow_label="WISPS")
