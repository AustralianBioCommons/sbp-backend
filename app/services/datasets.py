"""Dataset helpers — CSV generation and S3 upload for workflow samplesheets."""

from __future__ import annotations

import csv
import io
import json
import logging
import random
import re
import string
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from .s3 import S3UploadResult, upload_file_to_s3

logger = logging.getLogger(__name__)


def _stringify_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join("" if item is None else str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def build_unique_dataset_name(name: str) -> str:
    """Build a unique slug. E.g. 'my-run' -> 'my-run_20240101-120000_ab3x'"""
    base = name.strip()
    slug = re.sub(r"[^a-zA-Z0-9\-]", "-", base)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-") or "dataset"
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%d-%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{slug}_{ts}_{rand}"


def convert_form_data_to_csv(form_data: dict[str, Any]) -> str:
    """Convert a record of form data into a single-row CSV string."""
    if not form_data:
        raise ValueError("formData cannot be empty")

    headers = list(form_data.keys())
    row = [_stringify_field(form_data[key]) for key in headers]

    with io.StringIO() as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerow(row)
        return output.getvalue()


def _apply_bindcraft_design_target(
    form_data: dict[str, Any], workflow: str | None, tool: str | None
) -> None:
    """BindCraft's samplesheet requires number_of_final_designs, but the
    de-novo-design form only collects max_trajectories ("Number of
    Designs") — this derives the QC-pass target as 2x the trajectory
    count so the run isn't QC-gated below what was requested, without ever
    exposing it to the user. Scoped to workflow=de-novo-design AND
    tool=bindcraft specifically — de-novo-design also covers rfdiffusion,
    which has no samplesheet or trajectory-retry concept and must never have
    this derivation applied. Also protects other callers of this generic
    samplesheet builder (e.g. single-prediction), even if their own form data
    happened to contain a field with this name.
    """
    if workflow != "de-novo-design" or tool != "bindcraft" or "max_trajectories" not in form_data:
        return
    try:
        max_trajectories = int(str(form_data["max_trajectories"]).strip())
    except TypeError, ValueError:
        return
    form_data["number_of_final_designs"] = max_trajectories * 2


async def upload_csv_to_s3(
    form_data: dict[str, Any],
    settings: Settings | None = None,
    workflow: str | None = None,
    tool: str | None = None,
) -> S3UploadResult:
    """Generate a CSV from form_data and upload directly to S3."""
    if not form_data:
        raise ValueError("form_data cannot be empty")

    _apply_bindcraft_design_target(form_data, workflow, tool)
    csv_content = convert_form_data_to_csv(form_data)
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))

    logger.info("Uploading CSV samplesheet to S3")

    result = await upload_file_to_s3(
        file_content=file_bytes,
        filename="samplesheet.csv",
        content_type="text/csv",
        folder="inputs/samplesheets",
        settings=settings,
    )

    logger.info("CSV samplesheet uploaded to S3", extra={"s3Key": result.file_key})
    return result


async def upload_samplesheet_rows_to_s3(
    rows: list[dict[str, str]],
    settings: Settings | None = None,
) -> S3UploadResult:
    """Upload a multi-row samplesheet CSV (header taken from the first row's keys)."""
    if not rows:
        raise ValueError("rows cannot be empty")

    with io.StringIO() as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        csv_content = output.getvalue()

    return await upload_file_to_s3(
        file_content=io.BytesIO(csv_content.encode("utf-8")),
        filename="samplesheet.csv",
        content_type="text/csv",
        folder="inputs/samplesheets",
        settings=settings,
    )


async def upload_interaction_screening_samplesheet_to_s3(
    query_fasta_uri: str,
    target_fasta_uri: str,
    run_id: str,
    settings: Settings | None = None,
) -> S3UploadResult:
    """Build and upload the two-row interaction-screening samplesheet.

    Each row's ``sequence`` holds the S3 URI of that group's multi-FASTA; the
    launch endpoint stages both files to Gadi and rewrites the column to the
    local paths (see _stage_interaction_screening_fastas in routes/workflows.py).
    """
    if not run_id:
        raise ValueError("run_id is required")
    if not query_fasta_uri.strip() or not target_fasta_uri.strip():
        raise ValueError("queryFastaS3Uri and targetFastaS3Uri are required")

    rows = [
        {"id": "query", "sequence": query_fasta_uri.strip(), "group": "g1", "type": "protein"},
        {"id": "target", "sequence": target_fasta_uri.strip(), "group": "g2", "type": "protein"},
    ]

    logger.info("Uploading interaction-screening samplesheet to S3", extra={"runId": run_id})
    result = await upload_samplesheet_rows_to_s3(rows, settings=settings)
    logger.info(
        "interaction-screening samplesheet uploaded to S3", extra={"s3Key": result.file_key}
    )
    return result


async def upload_bulk_prediction_samplesheet_to_s3(
    fasta_uri: str,
    run_id: str,
    settings: Settings | None = None,
) -> S3UploadResult:
    """Build and upload the single-row bulk-prediction samplesheet.

    The row's ``sequence`` holds the S3 URI of the multi-FASTA; the launch
    endpoint stages it to Gadi and rewrites the column to the local path (see
    _stage_referenced_samplesheet_file in routes/workflows.py). The pipeline
    splits it into one interaction per FASTA record (``multi_fasta_file``).
    """
    if not run_id:
        raise ValueError("run_id is required")
    if not fasta_uri.strip():
        raise ValueError("fastaS3Uri is required")

    # WISPS ids must not contain "_" or "-"; per-record ids come from the FASTA headers.
    rows = [{"id": "bulk", "sequence": fasta_uri.strip(), "type": "protein"}]

    logger.info("Uploading bulk-prediction samplesheet to S3", extra={"runId": run_id})
    result = await upload_samplesheet_rows_to_s3(rows, settings=settings)
    logger.info("bulk-prediction samplesheet uploaded to S3", extra={"s3Key": result.file_key})
    return result
