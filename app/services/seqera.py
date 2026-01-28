"""Seqera Platform integration helpers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ..schemas.workflows import WorkflowLaunchForm

logger = logging.getLogger(__name__)


class SeqeraConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


class SeqeraServiceError(RuntimeError):
    """Raised when the Seqera Platform returns an error."""


@dataclass
class SeqeraLaunchResult:
    workflow_id: str
    status: str
    message: str | None = None


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


async def launch_seqera_workflow(
    form: WorkflowLaunchForm, dataset_id: str | None = None
) -> SeqeraLaunchResult:
    """Launch a workflow on the Seqera Platform."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")

    # Build default external parameters
    default_params = [
        "use_dgxa100: false",
        "validate_params: true",
        "help_full: false",
        'custom_config_base: "https://raw.githubusercontent.com/nf-core/configs/master"',
        "show_hidden: false",
        "plaintext_email: false",
        'project: "yz52"',
        "monochrome_logs: false",
        'error_strategy: "terminate"',
        "version: false",
        'custom_config_version: "master"',
        'outdir: "/g/data/za08/seqera-work/ui-jobs/"',
        "quote_char: '\"'",
        'bindcraft_container: "australianbiocommons/freebindcraft:1.0.3"',
        'publish_dir_mode: "copy"',
        'pipelines_testdata_base_path: "https://raw.githubusercontent.com/nf-core/test-datasets/"',
        "batches: 1",
        "help: false",
    ]

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
            "runName": form.runName or "hello-from-ui",
            "pipeline": form.pipeline or "https://github.com/nextflow-io/hello",
            "workDir": work_dir,
            "workspaceId": workspace_id,
            "revision": form.revision or "dev",
            "paramsText": params_text,
            "configProfiles": form.configProfiles or ["singularity"],
            "preRunScript": "module load nextflow",
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
        "Launching workflow via Seqera API",
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
        raise SeqeraServiceError(f"Seqera workflow launch failed: {response.status_code} {body}")

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    status = data.get("status", "submitted")

    if not workflow_id:
        raise SeqeraServiceError("Seqera workflow launch succeeded but did not return a workflowId")

    return SeqeraLaunchResult(
        workflow_id=workflow_id,
        status=status,
        message=data.get("message"),
    )
