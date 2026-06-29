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

from ..schemas.workflows import SequenceItem
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


INTERACTION_SCREENING_BASE_PATH = "/g/data/yz52/sbp-service/input/interaction_screening"


async def upload_csv_to_s3(
    form_data: dict[str, Any],
) -> S3UploadResult:
    """Generate a CSV from form_data and upload directly to S3."""
    if not form_data:
        raise ValueError("form_data cannot be empty")

    csv_content = convert_form_data_to_csv(form_data)
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))

    logger.info("Uploading CSV samplesheet to S3")

    result = await upload_file_to_s3(
        file_content=file_bytes,
        filename="samplesheet.csv",
        content_type="text/csv",
        folder="inputs/samplesheets",
    )

    logger.info("CSV samplesheet uploaded to S3", extra={"s3Key": result.file_key})
    return result


async def upload_interaction_screening_csv_to_s3(
    sequences: list[SequenceItem],
    run_id: str,
) -> tuple[S3UploadResult, str]:
    """Build and upload an interaction screening samplesheet directly to S3."""
    if not sequences:
        raise ValueError("sequences cannot be empty")
    if not run_id:
        raise ValueError("run_id is required")

    unique_run_path = build_unique_dataset_name(run_id)
    rows = [
        {
            "id": s.id,
            "sequence": f"{INTERACTION_SCREENING_BASE_PATH}/{unique_run_path}/{s.id}.fasta",
            "group": "g1" if s.group == "query" else "g2",
            "type": "protein",
        }
        for s in sequences
    ]

    with io.StringIO() as output:
        writer = csv.DictWriter(output, fieldnames=["id", "sequence", "group", "type"])
        writer.writeheader()
        writer.writerows(rows)
        csv_content = output.getvalue()

    file_bytes = io.BytesIO(csv_content.encode("utf-8"))
    split_output_dir = f"{INTERACTION_SCREENING_BASE_PATH}/{unique_run_path}"

    logger.info(
        "Uploading interaction screening samplesheet to S3",
        extra={"runId": run_id},
    )

    result = await upload_file_to_s3(
        file_content=file_bytes,
        filename="samplesheet.csv",
        content_type="text/csv",
        folder="inputs/samplesheets",
    )

    logger.info(
        "Interaction screening samplesheet uploaded to S3",
        extra={"s3Key": result.file_key, "splitOutputDir": split_output_dir},
    )

    return result, split_output_dir
