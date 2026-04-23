"""Proteinfold workflow executor for Seqera Platform (modeled after bindflow)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ..schemas.workflows import WorkflowLaunchForm
from .proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_default_params,
    get_proteinfold_executor_script,
)

logger = logging.getLogger(__name__)

class ProteinfoldConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""

class ProteinfoldExecutorError(RuntimeError):
    """Raised when proteinfold workflow execution fails."""

@dataclass
class ProteinfoldLaunchResult:
    workflow_id: str
    status: str
    message: str | None = None

def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ProteinfoldConfigurationError(f"Missing required environment variable: {key}")
    return value

async def launch_proteinfold_workflow(
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    pipeline: str,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str = "alphafold2",
) -> ProteinfoldLaunchResult:
    """Launch a proteinfold workflow on the Seqera Platform."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")
    s3_bucket = _get_required_env("AWS_S3_BUCKET")

    run_name = form.runName
    output_key = (output_id or "").strip()
    if not output_key:
        raise ProteinfoldConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{s3_bucket}/{output_key}"

    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_region = os.getenv("AWS_REGION", "ap-southeast-2")

    # Generate samplesheet URL from dataset_id (same as bindflow)
    samplesheet_url = f"{seqera_api_url}/workspaces/{workspace_id}/datasets/{dataset_id}/v/1/n/samplesheet.csv"

    # Build default parameters for proteinfold
    default_params = get_proteinfold_default_params(out_dir, samplesheet_url, mode)
    params_text = "\n".join(default_params)

    # Add custom paramsText from frontend if provided
    if form.paramsText and form.paramsText.strip():
        params_text = f"{params_text}\n{form.paramsText.rstrip()}"

    launch_payload: dict[str, Any] = {
        "launch": {
            "computeEnvId": compute_env_id,
            "runName": run_name,
            "pipeline": pipeline,
            "workDir": work_dir,
            "workspaceId": workspace_id,
            "revision": revision or "dev",
            "paramsText": params_text,
            "configProfiles": get_proteinfold_config_profiles(),
            "preRunScript": get_proteinfold_executor_script(
                aws_access_key, aws_secret_key, aws_region
            ),
            "resume": False,
        }
    }
    launch_payload["launch"]["datasetIds"] = [dataset_id]
    url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"

    logger.info("Launch payload paramsText", extra={"paramsText": params_text})
    logger.info("Full launch payload", extra={"payload": launch_payload})
    logger.info(
        "Launching proteinfold workflow via Seqera API",
        extra={
            "url": url,
            "workspaceId": workspace_id,
            "computeEnvId": compute_env_id,
            "pipeline": launch_payload["launch"]["pipeline"],
            "runName": launch_payload["launch"]["runName"],
        },
    )

    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.post(url, headers=headers, json=launch_payload)

    if response.is_error:
        body = response.text
        logger.error(
            "Seqera API error",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise ProteinfoldExecutorError(
            f"Proteinfold workflow launch failed: {response.status_code} {body}"
        )

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    status = data.get("status", "submitted")

    if not workflow_id:
        raise ProteinfoldExecutorError(
            "Proteinfold workflow launch succeeded but did not return a workflowId"
        )

    return ProteinfoldLaunchResult(
        workflow_id=workflow_id,
        status=status,
        message=data.get("message"),
    )
