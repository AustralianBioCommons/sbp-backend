"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml

from ..schemas.workflows import WorkflowFormData, WorkflowLaunchForm
from .wisps_config import (
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
    get_wisps_executor_script,
)

logger = logging.getLogger(__name__)


def _params_to_yaml_text(params: dict[str, Any]) -> str:
    if not params:
        return ""
    return str(yaml.dump(params, default_flow_style=False, sort_keys=False)).rstrip()


class WispsConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


class WispsExecutorError(RuntimeError):
    """Raised when WISPS workflow execution fails."""


@dataclass
class WispsLaunchResult:
    workflow_id: str
    status: str
    message: str | None = None


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise WispsConfigurationError(f"Missing required environment variable: {key}")
    return value


def _samplesheet_url(seqera_api_url: str, workspace_id: str, dataset_id: str) -> str:
    return (
        f"{seqera_api_url}/workspaces/{workspace_id}"
        f"/datasets/{dataset_id}/v/1/n/samplesheet.csv"
    )


async def _post_to_seqera(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> WispsLaunchResult:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.is_error:
        body = response.text
        logger.error(
            "Seqera API error %s %s: %s",
            response.status_code,
            response.reason_phrase,
            body,
        )
        raise WispsExecutorError(f"WISPS workflow launch failed: {response.status_code} {body}")

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    if not workflow_id:
        raise WispsExecutorError("WISPS workflow launch succeeded but did not return a workflowId")
    return WispsLaunchResult(
        workflow_id=workflow_id,
        status=data.get("status", "submitted"),
        message=data.get("message"),
    )


async def launch_wisps_workflow(
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    pipeline: str,
    config_path: str,
    form_data: WorkflowFormData,
    revision: str | None = None,
    output_id: str | None = None,
    user_email: str,
    full_name: str,
    institute: str,
    ip_address: str,
) -> WispsLaunchResult:
    """Launch an interaction screening (WISPS) workflow on the Seqera Platform."""
    fasta_s3_uri = str(
        getattr(form_data, "fastaS3Uri", None) or form_data.extra_fields.get("fastaS3Uri") or ""
    ).strip()
    split_output_dir = str(
        getattr(form_data, "splitOutputDir", None)
        or form_data.extra_fields.get("splitOutputDir")
        or ""
    ).strip()
    tool: str | None = form_data.tool or None

    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")

    if not output_id or not output_id.strip():
        raise WispsConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{_get_required_env('AWS_S3_BUCKET')}/{output_id.strip()}"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_id = (form.runName or "").strip()
    if not job_id:
        raise WispsConfigurationError("Missing run name for workflow launch")

    sheet_url = _samplesheet_url(seqera_api_url, workspace_id, dataset_id)
    params_text = _params_to_yaml_text(
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
        "launch": {
            "computeEnvId": compute_env_id,
            "runName": form.runName,
            "pipeline": pipeline,
            "workDir": work_dir,
            "workspaceId": workspace_id,
            "revision": revision or "main",
            "paramsText": params_text,
            "configProfiles": get_wisps_config_profiles(),
            "configText": config_text,
            "preRunScript": get_wisps_executor_script(
                fasta_s3_uri=fasta_s3_uri,
                split_output_dir=split_output_dir,
                aws_access_key=os.getenv("AWS_ACCESS_KEY_ID", ""),
                aws_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                aws_region=os.getenv("AWS_REGION", "ap-southeast-2"),
            ),
            "resume": False,
            "datasetIds": [dataset_id],
        }
    }

    launch_url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"
    logger.info("WISPS launch paramsText", extra={"paramsText": params_text})
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

    return await _post_to_seqera(
        launch_url,
        {
            "Authorization": f"Bearer {seqera_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        launch_payload,
    )
