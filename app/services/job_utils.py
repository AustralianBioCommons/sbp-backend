"""Helpers for job ownership, score handling, and Seqera payload parsing."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import RunMetric, RunOutput, S3Object, Workflow, WorkflowRun
from .s3 import (
    S3ConfigurationError,
    S3ServiceError,
    calculate_csv_column_max,
    generate_presigned_url,
    list_s3_files,
)

logger = logging.getLogger(__name__)


def coerce_workflow_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    workflow = payload.get("workflow")
    if isinstance(workflow, Mapping):
        return dict(workflow)
    return dict(payload)


def extract_pipeline_status(payload: Mapping[str, Any]) -> str:
    workflow = coerce_workflow_payload(payload)
    return str(workflow.get("status") or "UNKNOWN")


def parse_submit_datetime(payload: Mapping[str, Any]) -> datetime | None:
    workflow = coerce_workflow_payload(payload)
    submit_str = workflow.get("submit") or workflow.get("dateCreated")
    if not submit_str:
        return None
    try:
        return datetime.fromisoformat(str(submit_str).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_owned_run_ids(db: Session, user_id: UUID) -> set[str]:
    rows = db.execute(
        select(WorkflowRun.seqera_run_id).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.seqera_run_id.is_not(None),
        )
    ).all()
    return {row[0] for row in rows}


def get_owned_run(db: Session, user_id: UUID, run_id: str) -> WorkflowRun | None:
    return db.execute(
        select(WorkflowRun).where(
            WorkflowRun.owner_user_id == user_id,
            WorkflowRun.seqera_run_id == run_id,
        )
    ).scalar_one_or_none()


def _round_score(value: float | Decimal | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def get_score_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, float]:
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, RunMetric.max_score)
        .outerjoin(RunMetric, RunMetric.run_id == WorkflowRun.id)
        .where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {
        str(seqera_run_id): rounded
        for seqera_run_id, score in rows
        if seqera_run_id and (rounded := _round_score(score)) is not None
    }


def get_workflow_type_by_seqera_run_id(db: Session, user_id: UUID) -> dict[str, str]:
    """Return workflow type labels from the local DB workflows table."""
    rows = db.execute(
        select(WorkflowRun.seqera_run_id, Workflow.name)
        .outerjoin(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(WorkflowRun.owner_user_id == user_id)
    ).all()
    return {seqera_run_id: workflow_name for seqera_run_id, workflow_name in rows if workflow_name}


def _s3_uri_to_key(uri: str | None) -> str | None:
    if not uri:
        return None
    value = uri.strip()
    if not value:
        return None
    if not value.startswith("s3://"):
        return value
    # s3://bucket/key -> key
    parts = value.split("/", 3)
    if len(parts) < 4:
        return None
    return parts[3].strip() or None


def _get_sample_id_for_score(run: WorkflowRun) -> str | None:
    # Form schema `id` should be persisted on the run model as metadata.
    sample_id = (
        getattr(run, "sample_id", None)
        or getattr(run, "binder_name", None)
        or getattr(run, "form_id", None)
    )
    if not sample_id:
        return None
    value = str(sample_id).strip()
    return value or None


def _build_bindcraft_score_file_candidates(db: Session, run: WorkflowRun) -> list[str]:
    candidates: list[str] = []
    sample_id = _get_sample_id_for_score(run)
    prefixes: list[str] = []
    run_uuid = str(getattr(run, "id", "")).strip()
    if run_uuid:
        prefixes.append(run_uuid)
    seqera_run_id = str(getattr(run, "seqera_run_id", "")).strip()
    if seqera_run_id and seqera_run_id not in prefixes:
        prefixes.append(seqera_run_id)

    if sample_id:
        for key in (
            f"{sample_id}/ranker/{sample_id}_final_design_stats.csv",
            f"{sample_id}/{sample_id}_final_design_stats.csv",
        ):
            if key not in candidates:
                candidates.append(key)

        for prefix in prefixes:
            for key in (
                f"{prefix}/{sample_id}_final_design_stats.csv",
                f"{prefix}/ranker/{sample_id}_final_design_stats.csv",
            ):
                if key not in candidates:
                    candidates.append(key)

    rows = db.execute(
        select(S3Object.object_key, S3Object.uri)
        .join(RunOutput, RunOutput.s3_object_id == S3Object.object_key)
        .where(RunOutput.run_id == run.id)
    ).all()

    for object_key, uri in rows:
        for raw_key in (object_key, _s3_uri_to_key(uri)):
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not key:
                continue
            if (
                key.endswith("/ranker/s1_final_design_stats.csv")
                or key.endswith("s1_final_design_stats.csv")
                or key.endswith("_final_design_stats.csv")
            ):
                if key not in candidates:
                    candidates.append(key)

    for prefix in prefixes:
        fallback = f"results/{prefix}/ranker/s1_final_design_stats.csv"
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _get_run_output_keys(db: Session, run: WorkflowRun) -> list[str]:
    rows = db.execute(
        select(S3Object.object_key, S3Object.uri)
        .join(RunOutput, RunOutput.s3_object_id == S3Object.object_key)
        .where(RunOutput.run_id == run.id)
    ).all()
    keys: list[str] = []
    for object_key, uri in rows:
        for raw_key in (object_key, _s3_uri_to_key(uri)):
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
    sample_id = _get_sample_id_for_score(run)
    prefixes: list[str] = []
    run_uuid = str(getattr(run, "id", "") or "").strip()

    def _add(value: str) -> None:
        cleaned = value.strip().strip("/")
        if cleaned and cleaned not in prefixes:
            prefixes.append(cleaned)

    for identifier in (
        sample_id,
        str(getattr(run, "seqera_run_id", "") or "").strip(),
        str(getattr(run, "id", "") or "").strip(),
    ):
        if not identifier:
            continue
        _add(f"{identifier}/ranker")
        _add(f"{identifier}/Accepted/Animation")
        _add(f"results/{identifier}/ranker")
        _add(f"results/{identifier}/Accepted/Animation")

    if sample_id:
        _add(f"bindcraft/{sample_id}_0_output/Accepted/Animation")
        _add(f"{sample_id}_0_output/Accepted/Animation")
        _add(f"bindcraft/{sample_id}_0_output")
        _add(f"{sample_id}_0_output")
        if run_uuid:
            _add(f"{run_uuid}/bindcraft/{sample_id}_0_output/Accepted/Animation")
            _add(f"{run_uuid}/{sample_id}_0_output/Accepted/Animation")
            _add(f"{run_uuid}/bindcraft/{sample_id}_0_output")
            _add(f"{run_uuid}/{sample_id}_0_output")

    if run_uuid:
        _add(run_uuid)

    return [f"{prefix}/" for prefix in prefixes]


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
    """Return pre-signed links for the result artifacts shown in the UI."""
    await sync_bindcraft_outputs(db, run)
    matched: dict[str, tuple[str, str]] = {}

    for key in _get_run_output_keys(db, run):
        classified = _classify_bindcraft_output_key(key)
        if classified and key not in matched:
            matched[key] = classified

    found_categories = {category for category, _label in matched.values()}
    missing_categories = {"stats_csv", "pdb", "report", "snapshot"} - found_categories

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

    category_order = {"report": 0, "snapshot": 1, "stats_csv": 2, "pdb": 3}
    downloads: list[dict[str, str]] = []

    for key, (category, label) in sorted(
        matched.items(),
        key=lambda item: (category_order.get(item[1][0], 99), item[1][1].lower(), item[0]),
    ):
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
    await sync_bindcraft_outputs(db, run)
    report_keys: list[str] = []

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
        "url": await generate_presigned_url(report_key),
        "category": "report",
    }


async def get_result_snapshot_download(db: Session, run: WorkflowRun) -> dict[str, str] | None:
    """Return a single pre-signed snapshot image for the result view."""
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

    if not snapshot_keys:
        return None

    snapshot_key = sorted(snapshot_keys, key=lambda key: (key.rsplit("/", 1)[-1].lower(), key))[0]
    label = snapshot_key.rsplit("/", 1)[-1]
    return {
        "label": label,
        "key": snapshot_key,
        "url": await generate_presigned_url(snapshot_key),
        "category": "snapshot",
    }


async def ensure_completed_bindcraft_score(
    db: Session, run: WorkflowRun, ui_status: str
) -> float | None:
    if ui_status != "Completed":
        return None

    await sync_bindcraft_outputs(db, run)

    existing = db.execute(select(RunMetric).where(RunMetric.run_id == run.id)).scalar_one_or_none()
    if existing and existing.max_score is not None:
        return _round_score(existing.max_score)

    max_score: float | None = None
    for file_key in _build_bindcraft_score_file_candidates(db, run):
        try:
            max_score = await calculate_csv_column_max(
                file_key=file_key, column_name="Average_i_pTM"
            )
            break
        except (S3ConfigurationError, S3ServiceError, ValueError) as exc:
            logger.warning(
                "Failed to read score CSV candidate",
                extra={
                    "runId": str(run.id),
                    "seqeraRunId": run.seqera_run_id,
                    "fileKey": file_key,
                    "error": str(exc),
                },
            )
            continue
    if max_score is None:
        return None

    bounded_score = max(0.0, min(1.0, float(max_score)))
    if existing:
        existing.max_score = bounded_score
    else:
        db.add(RunMetric(run_id=run.id, max_score=bounded_score))
    db.commit()
    return _round_score(bounded_score)


# Backward-compatible alias. Prefer `ensure_completed_bindcraft_score`.
ensure_completed_run_score = ensure_completed_bindcraft_score
