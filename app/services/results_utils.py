"""Helpers for result-specific route responses and artifact discovery."""

from __future__ import annotations

import csv
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import StringIO
from typing import Any, Literal, Protocol, cast, get_args
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import RunOutput, S3Object, WorkflowRun
from ..schemas.workflows import ResultDownloadItem, ResultLogEntry, WorkflowName, WorkflowTool
from .s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    list_s3_files,
    read_s3_file,
)

OutputCategory = Literal["report", "stats_csv", "pdb", "snapshot", "alignment"]


class OutputClassifier(Protocol):
    """Function that classifies a workflow output key into a category."""

    def __call__(self, key: str, sample_id: str | None) -> ClassifiedOutput | None:
        ...


class GetScoreFile(Protocol):
    """Return the path to the score file for a workflow run."""
    def __call__(self, keys: list[str], sample_id: str | None) -> str | None:
        ...

@dataclass(frozen=True)
class ClassifiedOutput:
    category: OutputCategory
    label: str


@dataclass(frozen=True)
class WorkflowResultsSpec:
    """
    Defines the output categories for a workflow, along
    with functions to classify outputs into these categories
    """

    kind: WorkflowName
    tool: WorkflowTool
    required_categories: set[OutputCategory]
    get_prefixes: Callable[[WorkflowRun], list[str]]
    classify: OutputClassifier
    get_score_file: GetScoreFile
    extract_max_score: Callable[[str], Awaitable[float | None]]
    supports_snapshots: bool = False

    async def get_max_score(self, db: Session, run: WorkflowRun):
        keys = _get_run_output_keys(db, run)
        sample_id = get_sample_id_for_result(run)
        score_file = self.get_score_file(keys, sample_id)
        if score_file is None:
            return None
        return await self.extract_max_score(score_file)



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
    2. `run.submitted_form_data["sample_id"]`
    3. `run.submitted_form_data["id"]`
    4. `run.submitted_form_data["samplesheetId"]`
    5. `run.binder_name`
    6. `run.form_id`

    The first non-empty value is stripped and returned as a string. Returns `None`
    when all candidate fields are missing or blank.
    """
    sample_id = getattr(run, "sample_id", None)
    value = str(sample_id).strip() if sample_id is not None else ""
    if value:
        return value

    form_data = getattr(run, "submitted_form_data", None)
    if isinstance(form_data, dict):
        for key in ("sample_id", "id", "samplesheetId"):
            raw_form_id = form_data.get(key)
            form_id = str(raw_form_id).strip() if raw_form_id is not None else ""
            if form_id:
                return form_id

    sample_id = getattr(run, "binder_name", None) or getattr(run, "form_id", None)
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


def get_workflow_name(run: WorkflowRun) -> WorkflowName | None:
    if run.workflow is None:
        return None

    workflow_name: str = run.workflow.name
    assert workflow_name in get_args(WorkflowName), (
        f"Workflow name {workflow_name!r} not recognized: "
        f"expected one of {get_args(WorkflowName)}"
    )
    return cast(WorkflowName, workflow_name)


def get_tool_name(run: WorkflowRun) -> WorkflowTool | None:
    """
    Get the tool name for a workflow run.

    Prefer the
    """
    if run.tool is not None:
        tool = str(run.tool).strip().lower()
        if tool:
            return cast(WorkflowTool, tool)

    # Fall back to submitted form data if not available in run.tool
    form_data = getattr(run, "submitted_form_data", None)
    if isinstance(form_data, dict):
        for key in ("tool", "mode"):
            raw = form_data.get(key)
            if raw and str(raw).strip():
                return cast(WorkflowTool, str(raw).strip().lower())

    return None


def get_output_spec(run: WorkflowRun) -> WorkflowResultsSpec:
    workflow_name = get_workflow_name(run)
    if workflow_name is None:
        raise ValueError(f"Workflow name not found for run {run.id}")
    tool_name = get_tool_name(run)
    if tool_name is None:
        raise ValueError(f"Tool name not found for run {run.id}")

    spec = WORKFLOW_OUTPUT_SPECS.get(workflow_name, {}).get(tool_name)
    if spec is not None:
        return spec
    raise ValueError(
        f"Couldn't find a matching output spec: workflow={workflow_name!r}, tool={tool_name!r}"
    )


def classify_bindcraft_output_key(
    key: str, sample_id: str | None = None
) -> ClassifiedOutput | None:
    normalized = key.strip()
    if not normalized or normalized.endswith("/"):
        return None

    basename = normalized.rsplit("/", 1)[-1]
    lowered = normalized.lower()

    if basename.endswith("_final_design_stats.csv"):
        return ClassifiedOutput(category="stats_csv", label=basename)
    if "/generate/" in lowered and basename.lower().endswith(".html"):
        return ClassifiedOutput(category="report", label=basename)
    if "/bindcraft/" in lowered and "_0_output/" in lowered and basename.lower().endswith(".png"):
        return ClassifiedOutput(category="snapshot", label=basename)
    if "/ranker/" in lowered and "_ranked/" in lowered and basename.lower().endswith(".pdb"):
        return ClassifiedOutput(category="pdb", label=basename)
    return None


def get_bindcraft_score_file(keys: list[str], sample_id: str | None) -> str | None:
    for key in keys:
        normalized = key.strip()
        if not normalized:
            continue
        basename = normalized.rsplit("/", 1)[-1]
        if basename.endswith("_final_design_stats.csv"):
            return normalized
    return None


async def extract_bindcraft_max_score(score_file: str) -> float | None:
    content = await read_s3_file(score_file)
    csv_reader = csv.DictReader(StringIO(content))
    values: list[float] = []

    for row in csv_reader:
        value = row.get("Average_i_pTM")
        if value and value.strip():
            values.append(float(value))

    return max(values) if values else None


def get_proteinfold_score_file(keys: list[str], sample_id: str | None) -> str | None:
    sample_id_pattern = re.escape(sample_id) if sample_id else "single-prediction"
    score_pattern = rf"/{sample_id_pattern}/.+_ptm\.(tsv|csv)"
    for key in keys:
        if re.search(score_pattern, key):
            return key
    return None


async def extract_proteinfold_max_score(score_file: str) -> float | None:
    content = await read_s3_file(score_file)
    tsv = csv.reader(StringIO(content), delimiter="\t")
    # Max score should be the row with the lowest index
    max_row = min((row for row in tsv), key=lambda row: int(row[0]))
    return float(max_row[1])


def classify_proteinfold_output_key(
    key: str,
    *,
    pdb_pattern: str,
    stats_pattern: str,
    alignment_pattern: str | None = None,
) -> ClassifiedOutput | None:
    report_pattern = r"/reports/[^/]+_report\.html"
    normalized = key.strip()
    if not normalized or normalized.endswith("/"):
        return None
    filename = normalized.rsplit("/", 1)[-1]

    if re.search(report_pattern, normalized):
        return ClassifiedOutput(category="report", label=filename)
    if re.search(pdb_pattern, normalized):
        return ClassifiedOutput(category="pdb", label=filename)
    if re.search(stats_pattern, normalized):
        return ClassifiedOutput(category="stats_csv", label=filename)
    if alignment_pattern is not None and re.search(alignment_pattern, normalized):
        return ClassifiedOutput(category="alignment", label=filename)
    return None


def classify_boltz_proteinfold_output(
    key: str, sample_id: str | None = None
) -> ClassifiedOutput | None:
    sample_id_pattern = re.escape(sample_id) if sample_id else "single_prediction"
    return classify_proteinfold_output_key(
        key,
        pdb_pattern=rf"/boltz/top_ranked_structures/{sample_id_pattern}\.pdb",
        # Find across all subfolders
        stats_pattern=rf"/boltz/{sample_id_pattern}/.+\.tsv",
        alignment_pattern=rf"/mmseqs/{sample_id_pattern}\.a3m",
    )


def classify_alphafold2_proteinfold_output(
    key: str, sample_id: str | None = None
) -> ClassifiedOutput | None:
    sample_id_pattern = re.escape(sample_id) if sample_id else "single_prediction"
    return classify_proteinfold_output_key(
        key,
        pdb_pattern=rf"/alphafold2/split_msa_prediction/top_ranked_structures/{sample_id_pattern}\.pdb",
        stats_pattern=rf"/alphafold2/split_msa_prediction/{sample_id_pattern}/.+\.tsv",
    )


def classify_colabfold_proteinfold_output(
    key: str, sample_id: str | None = None
) -> ClassifiedOutput | None:
    sample_id_pattern = re.escape(sample_id) if sample_id else "single_prediction"
    return classify_proteinfold_output_key(
        key,
        pdb_pattern=rf"/colabfold/top_ranked_structures/{sample_id_pattern}\.pdb",
        stats_pattern=rf"/colabfold/{sample_id_pattern}/.+\.tsv",
        alignment_pattern=rf"/mmseqs/{sample_id_pattern}\.a3m",
    )


def build_bindcraft_output_listing_prefixes(run: WorkflowRun) -> list[str]:
    run_uuid = str(getattr(run, "id", "") or "").strip()
    if not run_uuid:
        return []

    # Always include run-UUID-only prefixes; these do not depend on sample_id.
    prefixes: list[str] = [
        f"{run_uuid}/ranker/",
        f"{run_uuid}/generate/",
    ]

    # Append bindcraft sample-specific prefixes only when a sample_id is available.
    sample_id = get_sample_id_for_result(run)
    if sample_id:
        prefixes.extend(
            [
                f"{run_uuid}/bindcraft/{sample_id}_0_output/",
            ]
        )

    return prefixes


def build_boltz_proteinfold_output_listing_prefixes(run: WorkflowRun) -> list[str]:
    run_uuid = str(getattr(run, "id", "") or "").strip()
    if not run_uuid:
        return []

    sample_id = get_sample_id_for_result(run)

    prefixes = [
        f"{run_uuid}/reports/",
        f"{run_uuid}/boltz/top_ranked_structures/",
        f"{run_uuid}/mmseqs/",
    ]

    if sample_id:
        prefixes.append(f"{run_uuid}/boltz/{sample_id}/")
    else:
        prefixes.append(f"{run_uuid}/boltz/")

    return prefixes


def build_alphafold2_proteinfold_output_listing_prefixes(run: WorkflowRun) -> list[str]:
    run_uuid = str(getattr(run, "id", "") or "").strip()
    if not run_uuid:
        return []

    sample_id = get_sample_id_for_result(run)

    prefixes = [
        f"{run_uuid}/reports/",
        f"{run_uuid}/alphafold2/split_msa_prediction/top_ranked_structures/",
    ]

    if sample_id:
        prefixes.append(f"{run_uuid}/alphafold2/split_msa_prediction/{sample_id}/")
    else:
        prefixes.append(f"{run_uuid}/alphafold2/split_msa_prediction/")

    return prefixes


def build_colabfold_proteinfold_output_listing_prefixes(run: WorkflowRun) -> list[str]:
    run_uuid = str(getattr(run, "id", "") or "").strip()
    if not run_uuid:
        return []

    sample_id = get_sample_id_for_result(run)

    prefixes = [
        f"{run_uuid}/reports/",
        f"{run_uuid}/colabfold/top_ranked_structures/",
        f"{run_uuid}/mmseqs/",
    ]

    if sample_id:
        prefixes.append(f"{run_uuid}/colabfold/{sample_id}/")
    else:
        prefixes.append(f"{run_uuid}/colabfold/")

    return prefixes


WORKFLOW_OUTPUT_SPECS: dict[WorkflowName, dict[WorkflowTool, WorkflowResultsSpec]] = {
    "de-novo-design": {
        "bindcraft": WorkflowResultsSpec(
            kind="de-novo-design",
            tool="bindcraft",
            required_categories={"report", "stats_csv", "pdb"},
            get_prefixes=build_bindcraft_output_listing_prefixes,
            get_score_file=get_bindcraft_score_file,
            extract_max_score=extract_bindcraft_max_score,
            classify=classify_bindcraft_output_key,
            supports_snapshots=True,
        ),
    },
    "single-prediction": {
        "boltz": WorkflowResultsSpec(
            kind="single-prediction",
            tool="boltz",
            required_categories={"report", "pdb", "stats_csv", "alignment"},
            get_prefixes=build_boltz_proteinfold_output_listing_prefixes,
            get_score_file=get_proteinfold_score_file,
            extract_max_score=extract_proteinfold_max_score,
            classify=classify_boltz_proteinfold_output,
        ),
        "alphafold2": WorkflowResultsSpec(
            kind="single-prediction",
            tool="alphafold2",
            required_categories={"report", "pdb", "stats_csv"},
            get_prefixes=build_alphafold2_proteinfold_output_listing_prefixes,
            get_score_file=get_proteinfold_score_file,
            extract_max_score=extract_proteinfold_max_score,
            classify=classify_alphafold2_proteinfold_output,
        ),
        "colabfold": WorkflowResultsSpec(
            kind="single-prediction",
            tool="colabfold",
            required_categories={"report", "pdb", "stats_csv", "alignment"},
            get_prefixes=build_colabfold_proteinfold_output_listing_prefixes,
            get_score_file=get_proteinfold_score_file,
            extract_max_score=extract_proteinfold_max_score,
            classify=classify_colabfold_proteinfold_output,
        ),
    },
}


def missing_required_categories(
    outputs: dict[str, ClassifiedOutput],
    spec: WorkflowResultsSpec,
) -> set[OutputCategory]:
    found = {output.category for output in outputs.values()}
    return set(spec.required_categories) - found


def collect_classified_outputs(
    db: Session,
    run: WorkflowRun,
    spec: WorkflowResultsSpec,
) -> dict[str, ClassifiedOutput]:
    outputs = {}
    sample_id = get_sample_id_for_result(run)
    for key in _get_run_output_keys(db, run):
        classified = spec.classify(key, sample_id)
        if classified:
            outputs[key] = classified
    return outputs


def _filter_outputs_by_category(
    outputs: dict[str, ClassifiedOutput],
    category: OutputCategory,
) -> dict[str, ClassifiedOutput]:
    return {key: output for key, output in outputs.items() if output.category == category}


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


async def list_workflow_outputs_from_s3(
    run: WorkflowRun,
    spec: WorkflowResultsSpec,
    *,
    suppress_s3_errors: bool = True,
) -> dict[str, ClassifiedOutput]:
    """Discover workflow result artifacts in S3"""
    outputs: dict[str, ClassifiedOutput] = {}
    sample_id = get_sample_id_for_result(run)

    for prefix in spec.get_prefixes(run):
        try:
            files = await list_s3_files(prefix=prefix)
        except (S3ConfigurationError, S3ServiceError) as exc:
            if suppress_s3_errors:
                logger.warning(
                    "Failed to list workflow outputs from S3",
                    extra={
                        "runId": str(run.id),
                        "seqeraRunId": run.seqera_run_id,
                        "workflowKind": spec.kind,
                        "workflowTool": spec.tool,
                        "prefix": prefix,
                        "error": str(exc),
                    },
                )
                continue
            raise

        for item in files:
            key = str(item.get("key", "")).strip()
            if not key or key in outputs:
                continue

            classified = spec.classify(key, sample_id)
            if classified is not None:
                outputs[key] = classified

    return outputs


async def sync_workflow_outputs(
    db: Session,
    run: WorkflowRun,
    spec: WorkflowResultsSpec,
    *,
    suppress_s3_errors: bool = True,
) -> list[str]:
    outputs = await list_workflow_outputs_from_s3(
        run,
        spec,
        suppress_s3_errors=suppress_s3_errors,
    )

    keys = list(outputs)
    if keys:
        _sync_run_output_records(db, run, keys)

    return keys


async def sync_bindcraft_outputs(db: Session, run: WorkflowRun) -> list[str]:
    """Discover bindcraft result artifacts in S3 and persist them as run outputs."""
    discovered: list[str] = []
    for prefix in build_bindcraft_output_listing_prefixes(run):
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
            if classify_bindcraft_output_key(key):
                discovered.append(key)

    if discovered:
        _sync_run_output_records(db, run, discovered)

    return discovered


def _get_output_sort_key(item: tuple[str, ClassifiedOutput]):
    """Sort output items by category, label, and key"""
    category_order = {"report": 0, "stats_csv": 1, "pdb": 2, "alignment": 3}
    key, output = item
    return (category_order.get(output.category, 99), output.label.lower(), key)


async def get_result_output_downloads(db: Session, run: WorkflowRun) -> list[ResultDownloadItem]:
    """Return pre-signed non-snapshot links for the result artifacts shown in the UI."""
    results_spec = get_output_spec(run)
    outputs = collect_classified_outputs(db, run, results_spec)

    if missing_required_categories(outputs, results_spec):
        await sync_workflow_outputs(db, run, results_spec)
        outputs = collect_classified_outputs(db, run, results_spec)

    if missing_required_categories(outputs, results_spec):
        discovered = await list_workflow_outputs_from_s3(
            run,
            results_spec,
            suppress_s3_errors=False,
        )
        outputs.update(discovered)

    downloads = []

    # Sort and filter outputs
    for key, output in sorted(outputs.items(), key=_get_output_sort_key):
        if output.category == "snapshot":
            continue
        downloads.append(
            ResultDownloadItem(
                label=output.label,
                key=key,
                url=await generate_presigned_url(key),
                category=output.category,
            )
        )

    return downloads


async def get_result_report_download(db: Session, run: WorkflowRun) -> ResultDownloadItem | None:
    """Return a single pre-signed HTML report link for the result view."""
    results_spec = get_output_spec(run)
    outputs = collect_classified_outputs(db, run, results_spec)
    report_outputs = _filter_outputs_by_category(outputs, "report")

    if not report_outputs:
        await sync_workflow_outputs(db, run, results_spec)
        outputs = collect_classified_outputs(db, run, results_spec)
        report_outputs = _filter_outputs_by_category(outputs, "report")

    if not report_outputs:
        discovered = await list_workflow_outputs_from_s3(
            run,
            results_spec,
            suppress_s3_errors=False,
        )
        outputs.update(discovered)
        report_outputs = _filter_outputs_by_category(outputs, "report")

    if not report_outputs:
        return None

    if len(report_outputs) > 1:
        raise ValueError(
            f"Multiple report outputs found for run {run.id!r}: {report_outputs.keys()}"
        )
    report_key, report_output = report_outputs.popitem()
    return ResultDownloadItem(
        label=report_output.label,
        key=report_key,
        url=await generate_presigned_url(
            report_key,
            response_content_type="text/html",
            response_content_disposition="inline",
        ),
        category=report_output.category,
    )


async def get_result_snapshot_downloads(db: Session, run: WorkflowRun) -> list[ResultDownloadItem]:
    """Return pre-signed snapshot image links for the result view."""
    results_spec = get_output_spec(run)
    if not results_spec.supports_snapshots:
        return []

    outputs = collect_classified_outputs(db, run, results_spec)
    snapshot_outputs = _filter_outputs_by_category(outputs, "snapshot")

    if not snapshot_outputs:
        await sync_workflow_outputs(db, run, results_spec)
        outputs = collect_classified_outputs(db, run, results_spec)
        snapshot_outputs = _filter_outputs_by_category(outputs, "snapshot")

    if not snapshot_outputs:
        discovered = await list_workflow_outputs_from_s3(
            run,
            results_spec,
            suppress_s3_errors=False,
        )
        outputs.update(discovered)
        snapshot_outputs = _filter_outputs_by_category(outputs, "snapshot")

    downloads = []
    for snapshot_key, snapshot_output in sorted(snapshot_outputs.items(), key=_get_output_sort_key):
        downloads.append(
            ResultDownloadItem(
                label=snapshot_output.label,
                key=snapshot_key,
                url=await generate_presigned_url(snapshot_key),
                category=snapshot_output.category,
            )
        )
    return downloads
