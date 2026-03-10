"""Helpers for result-specific route responses."""

from __future__ import annotations

from typing import Any

from ..db.models.core import WorkflowRun


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
