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


def _yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{value}"'


def _tool_param_lines(form_data: dict[str, Any]) -> list[str]:
    return [
        f"{key}: {_yaml_value(form_data[key])}"
        for key in _TOOL_PARAM_KEYS
        if key in form_data and form_data[key] is not None
    ]


class ProteinfoldConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


class ProteinfoldExecutorError(RuntimeError):
    """Raised when proteinfold workflow execution fails."""


@dataclass
class ProteinfoldLaunchResult:
    """Result of a proteinfold workflow launch."""

    workflow_id: str
    status: str
    message: str | None = None


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ProteinfoldConfigurationError(f"Missing required environment variable: {key}")
    return value


def _samplesheet_url(seqera_api_url: str, workspace_id: str, dataset_id: str) -> str:
    return (
        f"{seqera_api_url}/workspaces/{workspace_id}"
        f"/datasets/{dataset_id}/v/1/n/samplesheet.csv"
    )


def _build_params_text(
    out_dir: str,
    samplesheet_url: str,
    mode: str,
    form_data: dict[str, Any] | None,
    custom_params: str | None,
) -> str:
    """Build the YAML params string for the Seqera launch payload."""
    lines = get_proteinfold_default_params(out_dir, samplesheet_url, mode)
    if form_data:
        lines.extend(_tool_param_lines(form_data))
    params_text = "\n".join(lines)
    if custom_params and custom_params.strip():
        params_text = f"{params_text}\n{custom_params.rstrip()}"
    return params_text


async def _post_to_seqera(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> ProteinfoldLaunchResult:
    """Send the launch request to Seqera and return the result."""
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
        raise ProteinfoldExecutorError(
            f"Proteinfold workflow launch failed: {response.status_code} {body}"
        )

    data = response.json()
    workflow_id = data.get("workflowId") or data.get("data", {}).get("workflowId")
    if not workflow_id:
        raise ProteinfoldExecutorError(
            "Proteinfold workflow launch succeeded but did not return a workflowId"
        )
    return ProteinfoldLaunchResult(
        workflow_id=workflow_id,
        status=data.get("status", "submitted"),
        message=data.get("message"),
    )


async def launch_proteinfold_workflow(
    form: WorkflowLaunchForm,
    dataset_id: str,
    *,
    pipeline: str,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str = "alphafold2",
    form_data: dict[str, Any] | None = None,
) -> ProteinfoldLaunchResult:
    """Launch a proteinfold workflow on the Seqera Platform."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")
    compute_env_id = _get_required_env("COMPUTE_ID")
    work_dir = _get_required_env("WORK_DIR")

    if not output_id or not output_id.strip():
        raise ProteinfoldConfigurationError("Missing output identifier for workflow launch")
    out_dir = f"s3://{_get_required_env('AWS_S3_BUCKET')}/{output_id.strip()}"

    sheet_url = _samplesheet_url(seqera_api_url, workspace_id, dataset_id)
    params_text = _build_params_text(
        out_dir,
        sheet_url,
        mode,
        form_data,
        form.paramsText,
    )

    launch_payload: dict[str, Any] = {
        "launch": {
            "computeEnvId": compute_env_id,
            "runName": form.runName,
            "pipeline": pipeline,
            "workDir": work_dir,
            "workspaceId": workspace_id,
            "revision": revision or "dev",
            "paramsText": params_text,
            "configProfiles": get_proteinfold_config_profiles(),
            "preRunScript": get_proteinfold_executor_script(
                os.getenv("AWS_ACCESS_KEY_ID", ""),
                os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                os.getenv("AWS_REGION", "ap-southeast-2"),
            ),
            "resume": False,
            "datasetIds": [dataset_id],
        }
    }

    launch_url = f"{seqera_api_url}/workflow/launch?workspaceId={workspace_id}"
    logger.info("Launch payload paramsText", extra={"paramsText": params_text})
    logger.info("Full launch payload", extra={"payload": launch_payload})
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

    return await _post_to_seqera(
        launch_url,
        {
            "Authorization": f"Bearer {seqera_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        launch_payload,
    )
