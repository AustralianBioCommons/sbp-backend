"""Helpers for result-specific route responses and artifact discovery."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import RunOutput, S3Object, WorkflowRun
from ..schemas.workflows import ResultLogEntry
from .s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    list_s3_files,
)

_LOG_LEVEL_PATTERN = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
_LOG_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?)\s*(?P<rest>.*)$"
)
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_HEADER_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f"\\]+')
_FILENAME_FALLBACK_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

logger = logging.getLogger(__name__)


def _sanitize_content_disposition_filename(filename: str) -> str:
    sanitized = _HEADER_UNSAFE_FILENAME_CHARS.sub("_", filename).strip()
    return sanitized or "download"


def _format_attachment_content_disposition(filename: str) -> str:
    sanitized = _sanitize_content_disposition_filename(filename)
    ascii_fallback = sanitized.encode("ascii", "ignore").decode("ascii")
    ascii_fallback = _FILENAME_FALLBACK_UNSAFE_CHARS.sub("_", ascii_fallback).strip("._")
    ascii_fallback = ascii_fallback or "download"
    encoded_filename = quote(sanitized, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}"


def resolve_submitted_form_data(run: WorkflowRun) -> dict[str, Any] | None:
    """Resolve settings for the results view from stored form data or local fallback fields.

    Preferred source is `workflow_runs.submitted_form_data`, which contains the original
    workflow form payload. When that payload is missing (for older runs), this function
    reconstructs a minimal settings dictionary from local columns (`sample_id`,
    `binder_name`, and `run_metrics.final_design_count`) and includes explicit metadata:
    `_source="fallback_local"` and `_warning="submitted_form_data_missing"`.

    Returns `None` only when neither stored form data nor fallback fields are available.
    """
    stored = getattr(run, "submitted_form_data", None)
    if isinstance(stored, dict):
        return stored

    fallback: dict[str, Any] = {}
    sample_id = getattr(run, "sample_id", None)
    binder_name = getattr(run, "binder_name", None)
    metrics = getattr(run, "metrics", None)

    if sample_id:
        fallback["id"] = sample_id
    if binder_name:
        fallback["binder_name"] = binder_name
    final_design_count = getattr(metrics, "final_design_count", None)
    if final_design_count is not None:
        fallback["number_of_final_designs"] = final_design_count

    if fallback:
        fallback["_source"] = "fallback_local"
        fallback["_warning"] = "submitted_form_data_missing"

    return fallback or None


async def resolve_pdb_presigned_urls(
    form_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Replace the ``starting_pdb`` S3 URI in form data with a presigned download URL.

    If the ``starting_pdb`` field contains an ``s3://`` URI, it is resolved to a
    time-limited presigned HTTPS download URL. All other fields are returned
    unchanged. S3 errors are silently suppressed so the rest of the settings
    remain visible even when presigning fails.
    """
    if not form_data:
        return form_data

    pdb_value = form_data.get("starting_pdb")
    if not isinstance(pdb_value, str) or not pdb_value.startswith("s3://"):
        return form_data

    file_key = s3_uri_to_key(pdb_value)
    if not file_key:
        return form_data

    filename = file_key.rsplit("/", 1)[-1] if "/" in file_key else file_key
    try:
        presigned_url = await generate_presigned_url(
            file_key=file_key,
            expiration=3600,
            response_content_disposition=_format_attachment_content_disposition(filename),
        )
        return {**form_data, "starting_pdb": presigned_url}
    except (S3ConfigurationError, S3ServiceError):
        logger.warning(
            "Failed to generate presigned starting_pdb URL for S3 key %r; "
            "returning original form data",
            file_key,
            exc_info=True,
        )
        return form_data


def format_log_entries(entries: list[str] | None) -> list[ResultLogEntry]:
    """Normalize raw Seqera log lines for frontend display."""
    formatted: list[ResultLogEntry] = []
    for index, raw_entry in enumerate(entries or []):
        raw = str(raw_entry)
        line = _ANSI_ESCAPE_PATTERN.sub("", raw).strip()
        timestamp: str | None = None
        message = line

        timestamp_match = _LOG_TIMESTAMP_PATTERN.match(line)
        if timestamp_match:
            timestamp = timestamp_match.group("timestamp")
            message = timestamp_match.group("rest").strip() or line

        level = "INFO"
        level_match = _LOG_LEVEL_PATTERN.search(message)
        if level_match:
            matched_level = level_match.group(1)
            level = "WARN" if matched_level == "WARNING" else matched_level

        formatted.append(
            ResultLogEntry(
                index=index,
                raw=raw,
                message=message or raw,
                level=level,
                timestamp=timestamp,
            )
        )
    return formatted


def s3_uri_to_key(uri: str | None) -> str | None:
    if not uri:
        return None
    value = uri.strip()
    if not value:
        return None
    if not value.startswith("s3://"):
        return value
    parts = value.split("/", 3)
    if len(parts) < 4:
        return None
    return parts[3].strip() or None


def get_sample_id_for_result(run: WorkflowRun) -> str | None:
    """Return the best available result identifier for artifact path discovery.

    The lookup order is:
    1. `run.sample_id`
    2. `run.binder_name`
    3. `run.form_id`

    The first non-empty value is stripped and returned as a string. Returns `None`
    when all candidate fields are missing or blank.
    """
    sample_id = (
        getattr(run, "sample_id", None)
        or getattr(run, "binder_name", None)
        or getattr(run, "form_id", None)
    )
    value = str(sample_id).strip() if sample_id is not None else ""
    return value or None


def _get_run_output_keys(db: Session, run: WorkflowRun) -> list[str]:
    rows = db.execute(
        select(S3Object.object_key, S3Object.uri)
        .join(RunOutput, RunOutput.s3_object_id == S3Object.object_key)
        .where(RunOutput.run_id == run.id)
    ).all()
    keys: list[str] = []
    for object_key, uri in rows:
        for raw_key in (object_key, s3_uri_to_key(uri)):
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _classify_bindcraft_output_key(key: str) -> tuple[str, str] | None:
    normalized = key.strip()
    if not normalized or normalized.endswith("/"):
        return None

    basename = normalized.rsplit("/", 1)[-1]
    lowered = normalized.lower()

    if basename.endswith("_final_design_stats.csv"):
        return ("stats_csv", basename)
    if "/accepted/animation/" in lowered and basename.lower().endswith(".html"):
        return ("report", basename)
    if "/bindcraft/" in lowered and "_0_output/" in lowered and basename.lower().endswith(".png"):
        return ("snapshot", basename)
    if "/ranker/" in lowered and "_ranked/" in lowered and basename.lower().endswith(".pdb"):
        return ("pdb", basename)
    return None


def _build_bindcraft_output_listing_prefixes(run: WorkflowRun) -> list[str]:
    run_uuid = str(getattr(run, "id", "") or "").strip()
    if not run_uuid:
        return []

    # Always include run-UUID-only prefixes; these do not depend on sample_id.
    prefixes: list[str] = [
        f"{run_uuid}/ranker/",
        f"{run_uuid}/Accepted/Animation/",
    ]

    # Append bindcraft sample-specific prefixes only when a sample_id is available.
    sample_id = get_sample_id_for_result(run)
    if sample_id:
        prefixes.extend(
            [
                f"{run_uuid}/bindcraft/{sample_id}_0_output/Accepted/Animation/",
                f"{run_uuid}/bindcraft/{sample_id}_0_output/",
            ]
        )

    return prefixes


def _build_s3_uri(key: str) -> str:
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if bucket_name:
        return f"s3://{bucket_name}/{key}"
    return key


def _sync_run_output_records(db: Session, run: WorkflowRun, keys: list[str]) -> None:
    existing_keys = set(_get_run_output_keys(db, run))
    changed = False

    for key in keys:
        normalized = key.strip()
        if not normalized or normalized in existing_keys:
            continue

        s3_object = db.get(S3Object, normalized)
        if s3_object is None:
            s3_object = S3Object(
                object_key=normalized,
                uri=_build_s3_uri(normalized),
            )
            db.add(s3_object)

        db.add(RunOutput(run_id=run.id, s3_object_id=normalized))
        existing_keys.add(normalized)
        changed = True

    if changed:
        db.commit()


async def sync_bindcraft_outputs(db: Session, run: WorkflowRun) -> list[str]:
    """Discover bindcraft result artifacts in S3 and persist them as run outputs."""
    discovered: list[str] = []
    for prefix in _build_bindcraft_output_listing_prefixes(run):
        try:
            files = await list_s3_files(prefix=prefix)
        except (S3ConfigurationError, S3ServiceError) as exc:
            logger.warning(
                "Failed to list bindcraft outputs from S3",
                extra={
                    "runId": str(run.id),
                    "seqeraRunId": run.seqera_run_id,
                    "prefix": prefix,
                    "error": str(exc),
                },
            )
            continue
        for item in files:
            key = str(item.get("key", "")).strip()
            if not key or key in discovered:
                continue
            if _classify_bindcraft_output_key(key):
                discovered.append(key)

    if discovered:
        _sync_run_output_records(db, run, discovered)

    return discovered


async def get_result_output_downloads(db: Session, run: WorkflowRun) -> list[dict[str, str]]:
    """Return pre-signed non-snapshot links for the result artifacts shown in the UI."""
    matched: dict[str, tuple[str, str]] = {}

    for key in _get_run_output_keys(db, run):
        classified = _classify_bindcraft_output_key(key)
        if classified and key not in matched:
            matched[key] = classified

    found_categories = {category for category, _label in matched.values()}
    missing_categories = {"stats_csv", "pdb", "report"} - found_categories

    if missing_categories:
        await sync_bindcraft_outputs(db, run)
        for key in _get_run_output_keys(db, run):
            classified = _classify_bindcraft_output_key(key)
            if classified and key not in matched:
                matched[key] = classified
        found_categories = {category for category, _label in matched.values()}
        missing_categories = {"stats_csv", "pdb", "report"} - found_categories

    if missing_categories:
        for prefix in _build_bindcraft_output_listing_prefixes(run):
            files = await list_s3_files(prefix=prefix)
            for item in files:
                key = str(item.get("key", "")).strip()
                if not key or key in matched:
                    continue
                classified = _classify_bindcraft_output_key(key)
                if classified:
                    matched[key] = classified

    category_order = {"report": 0, "stats_csv": 1, "pdb": 2}
    downloads: list[dict[str, str]] = []

    for key, (category, label) in sorted(
        matched.items(),
        key=lambda item: (category_order.get(item[1][0], 99), item[1][1].lower(), item[0]),
    ):
        if category == "snapshot":
            continue
        downloads.append(
            {
                "label": label,
                "key": key,
                "url": await generate_presigned_url(key),
                "category": category,
            }
        )

    return downloads


async def get_result_report_download(db: Session, run: WorkflowRun) -> dict[str, str] | None:
    """Return a single pre-signed HTML report link for the result view."""
    report_keys: list[str] = []

    for key in _get_run_output_keys(db, run):
        classified = _classify_bindcraft_output_key(key)
        if classified and classified[0] == "report" and key not in report_keys:
            report_keys.append(key)

    if not report_keys:
        await sync_bindcraft_outputs(db, run)
        for key in _get_run_output_keys(db, run):
            classified = _classify_bindcraft_output_key(key)
            if classified and classified[0] == "report" and key not in report_keys:
                report_keys.append(key)

    if not report_keys:
        for prefix in _build_bindcraft_output_listing_prefixes(run):
            files = await list_s3_files(prefix=prefix)
            for item in files:
                key = str(item.get("key", "")).strip()
                if not key or key in report_keys:
                    continue
                classified = _classify_bindcraft_output_key(key)
                if classified and classified[0] == "report":
                    report_keys.append(key)

    if not report_keys:
        return None

    report_key = sorted(report_keys, key=lambda key: (key.rsplit("/", 1)[-1].lower(), key))[0]
    label = report_key.rsplit("/", 1)[-1]
    return {
        "label": label,
        "key": report_key,
        "url": await generate_presigned_url(
            report_key,
            response_content_type="text/html",
            response_content_disposition="inline",
        ),
        "category": "report",
    }


async def get_result_snapshot_downloads(db: Session, run: WorkflowRun) -> list[dict[str, str]]:
    """Return pre-signed snapshot image links for the result view."""
    await sync_bindcraft_outputs(db, run)
    snapshot_keys: list[str] = []

    for key in _get_run_output_keys(db, run):
        classified = _classify_bindcraft_output_key(key)
        if classified and classified[0] == "snapshot" and key not in snapshot_keys:
            snapshot_keys.append(key)

    if not snapshot_keys:
        for prefix in _build_bindcraft_output_listing_prefixes(run):
            files = await list_s3_files(prefix=prefix)
            for item in files:
                key = str(item.get("key", "")).strip()
                if not key or key in snapshot_keys:
                    continue
                classified = _classify_bindcraft_output_key(key)
                if classified and classified[0] == "snapshot":
                    snapshot_keys.append(key)

    downloads: list[dict[str, str]] = []
    for snapshot_key in sorted(
        snapshot_keys, key=lambda key: (key.rsplit("/", 1)[-1].lower(), key)
    ):
        downloads.append(
            {
                "label": snapshot_key.rsplit("/", 1)[-1],
                "key": snapshot_key,
                "url": await generate_presigned_url(snapshot_key),
                "category": "snapshot",
            }
        )
    return downloads
