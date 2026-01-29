"""Bindflow workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ..schemas.workflows import WorkflowLaunchForm
from .bindflow_config import (
    get_bindflow_config_profiles,
    get_bindflow_default_params,
    get_bindflow_executor_script,
)

logger = logging.getLogger(__name__)


class BindflowConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


class BindflowExecutorError(RuntimeError):
    """Raised when bindflow workflow execution fails."""


@dataclass
class BindflowLaunchResult:
    workflow_id: str
    status: str
    message: str | None = None


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise BindflowConfigurationError(f"Missing required environment variable: {key}")
    return value


async def launch_bindflow_workflow(
    form: WorkflowLaunchForm, dataset_id: str | None = None
) -> BindflowLaunchResult:
    """Launch a bindflow workflow on the Seqera Platform."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")
    s3_bucket = _get_required_env("AWS_S3_BUCKET")

    # Get run name and include it in output directory
    run_name = form.runName or "hello-from-ui"
    out_dir = f"s3://{s3_bucket}/{run_name}"

    # Get AWS credentials from backend env (if available)
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_region = os.getenv("AWS_REGION", "ap-southeast-2")

    # Build default external parameters from config
    default_params = get_bindflow_default_params(out_dir)

    # Start with default parameters
    params_text = "\n".join(default_params)

    # Add custom paramsText from frontend if provided
    if form.paramsText and form.paramsText.strip():
        params_text = f"{params_text}\n{form.paramsText.rstrip()}"

    # Add dataset input URL if dataset_id is provided
    if dataset_id:
        dataset_url = f"{seqera_api_url}/workspaces/{workspace_id}/datasets/{dataset_id}/v/1/n/samplesheet.csv"
        params_text = f"{params_text}\ninput: {dataset_url}"

    launch_payload: dict[str, Any] = {
        "launch": {
            "computeEnvId": compute_env_id,
            "runName": run_name,
            "pipeline": form.pipeline,
            "workDir": work_dir,
            "workspaceId": workspace_id,
            "revision": form.revision or "dev",
            "paramsText": params_text,
            "configProfiles": get_bindflow_config_profiles(),
            "preRunScript": get_bindflow_executor_script(aws_access_key, aws_secret_key, aws_region),
            "resume": False,
        }
    }

    if dataset_id:
        launch_payload["launch"]["datasetIds"] = [dataset_id]

    url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"

    # Log the complete params being sent
    logger.info("Launch payload paramsText", extra={"paramsText": params_text})

    logger.info("Full launch payload", extra={"payload": launch_payload})

    logger.info(
        "Launching bindflow workflow via Seqera API",
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
        raise BindflowExecutorError(f"Bindflow workflow launch failed: {response.status_code} {body}")

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    status = data.get("status", "submitted")

    if not workflow_id:
        raise BindflowExecutorError("Bindflow workflow launch succeeded but did not return a workflowId")

    return BindflowLaunchResult(
        workflow_id=workflow_id,
        status=status,
        message=data.get("message"),
    )
