"""Dataset helpers for interacting with the Seqera Platform."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .bindflow_executor import BindflowConfigurationError, BindflowExecutorError

logger = logging.getLogger(__name__)


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise BindflowConfigurationError(f"Missing required environment variable: {key}")
    return value


def _stringify_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join("" if item is None else str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def convert_form_data_to_csv(form_data: dict[str, Any]) -> str:
    """Convert a record of form data into a single-row CSV string."""
    if not form_data:
        raise ValueError("formData cannot be empty")

    headers = list(form_data.keys())
    row = [_stringify_field(form_data[key]) for key in headers]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerow(row)
    return output.getvalue()


@dataclass
class DatasetCreationResult:
    dataset_id: str
    raw_response: dict[str, Any]


@dataclass
class DatasetUploadResult:
    success: bool
    dataset_id: str
    message: str
    raw_response: dict[str, Any] | None = None


async def create_seqera_dataset(
    name: str | None = None, description: str | None = None
) -> DatasetCreationResult:
    """Create a dataset on the Seqera Platform."""
    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")

    dataset_name = name or f"dataset-{int(time.time() * 1000)}"
    payload = {
        "name": dataset_name,
        "description": description or "Dataset for workflow submission",
    }

    url = f"{seqera_api_url}/workspaces/{workspace_id}/datasets/"
    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "Creating Seqera dataset",
        extra={"url": url, "workspaceId": workspace_id, "datasetName": dataset_name},
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.is_error:
        body = response.text
        logger.error(
            "Seqera dataset creation failed",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise BindflowExecutorError(
            f"Seqera dataset creation failed: {response.status_code} {body}"
        )

    data = response.json()
    dataset_id = data.get("dataset", {}).get("id")
    if not dataset_id:
        raise BindflowExecutorError(
            "Seqera dataset creation succeeded but response lacked dataset id"
        )

    logger.info("Seqera dataset created", extra={"datasetId": dataset_id})
    return DatasetCreationResult(dataset_id=dataset_id, raw_response=data)


async def upload_dataset_to_seqera(
    dataset_id: str, form_data: dict[str, Any]
) -> DatasetUploadResult:
    """Upload CSV-encoded form data to an existing Seqera dataset."""
    if not dataset_id:
        raise ValueError("dataset_id is required")
    if not form_data:
        raise ValueError("formData cannot be empty")

    seqera_api_url = _get_required_env("SEQERA_API_URL").rstrip("/")
    seqera_token = _get_required_env("SEQERA_ACCESS_TOKEN")
    workspace_id = _get_required_env("WORK_SPACE")

    csv_payload = convert_form_data_to_csv(form_data)
    url = f"{seqera_api_url}/workspaces/{workspace_id}/datasets/{dataset_id}/upload"
    headers = {
        "Authorization": f"Bearer {seqera_token}",
        "Accept": "application/json",
    }

    logger.info(
        "Uploading dataset to Seqera",
        extra={"datasetId": dataset_id, "workspaceId": workspace_id, "url": url},
    )

    files = {
        "file": ("samplesheet.csv", csv_payload, "text/csv"),
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        response = await client.post(url, headers=headers, files=files)

    if response.is_error:
        body = response.text
        logger.error(
            "Seqera dataset upload failed",
            extra={
                "status": response.status_code,
                "reason": response.reason_phrase,
                "body": body,
            },
        )
        raise BindflowExecutorError(f"Seqera dataset upload failed: {response.status_code} {body}")

    data = response.json()
    returned_dataset_id = data.get("version", {}).get("datasetId") or dataset_id
    message = data.get("message") or "Upload successful"

    logger.info(
        "Seqera dataset upload completed",
        extra={"datasetId": returned_dataset_id, "status": response.status_code},
    )

    return DatasetUploadResult(
        success=True,
        dataset_id=returned_dataset_id,
        message=message,
        raw_response=data,
    )
