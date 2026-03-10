"""Helpers for result-specific route responses."""

from __future__ import annotations

import re
from typing import Any

from ..db.models.core import WorkflowRun
from ..schemas.workflows import ResultLogEntry

_LOG_LEVEL_PATTERN = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
_LOG_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?)\s*(?P<rest>.*)$"
)


def resolve_submitted_form_data(run: WorkflowRun) -> dict[str, Any] | None:
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
    return fallback or None


def format_log_entries(entries: list[str] | None) -> list[ResultLogEntry]:
    """Normalize raw Seqera log lines for frontend display."""
    formatted: list[ResultLogEntry] = []
    for index, raw_entry in enumerate(entries or []):
        raw = str(raw_entry)
        line = raw.strip()
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
