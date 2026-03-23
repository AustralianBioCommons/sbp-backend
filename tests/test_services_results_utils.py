"""Tests for result-specific formatting helpers."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.db.models.core import WorkflowRun
from app.services.results_utils import (
    _build_bindcraft_output_listing_prefixes,
    _build_s3_uri,
    _classify_bindcraft_output_key,
    format_log_entries,
    get_sample_id_for_result,
    resolve_submitted_form_data,
    s3_uri_to_key,
)


def test_format_log_entries_extracts_timestamp_level_and_strips_ansi():
    result = format_log_entries(
        [
            "2026-03-10T10:00:00Z INFO Starting workflow",
            "plain line without metadata",
            "2026-03-10 10:01:00 WARNING Queue is full",
            "  \u001b[0;34mworkDir                   : \u001b[0;32m/scratch/yz52/sbp/workdir\u001b[0m",
        ]
    )

    assert len(result) == 4
    assert result[0].timestamp == "2026-03-10T10:00:00Z"
    assert result[0].level == "INFO"
    assert result[0].message == "INFO Starting workflow"
    assert result[1].timestamp is None
    assert result[1].level == "INFO"
    assert result[1].message == "plain line without metadata"
    assert result[2].level == "WARN"
    assert "\u001b[" not in result[3].message
    assert result[3].message == "workDir                   : /scratch/yz52/sbp/workdir"


def test_resolve_submitted_form_data_prefers_stored_payload():
    run = SimpleNamespace(submitted_form_data={"id": "stored", "binder_name": "binder"})

    assert resolve_submitted_form_data(run) == {"id": "stored", "binder_name": "binder"}


def test_resolve_submitted_form_data_builds_fallback_payload():
    run = SimpleNamespace(
        submitted_form_data=None,
        sample_id="sample-1",
        binder_name="binder-a",
        metrics=SimpleNamespace(final_design_count=7),
    )

    assert resolve_submitted_form_data(run) == {
        "id": "sample-1",
        "binder_name": "binder-a",
        "number_of_final_designs": 7,
        "_source": "fallback_local",
        "_warning": "submitted_form_data_missing",
    }


def test_resolve_submitted_form_data_returns_none_when_nothing_available():
    run = SimpleNamespace(submitted_form_data=None, sample_id=None, binder_name=None, metrics=None)

    assert resolve_submitted_form_data(run) is None


def test_s3_uri_to_key_handles_empty_non_s3_and_invalid_s3_values():
    assert s3_uri_to_key(None) is None
    assert s3_uri_to_key("   ") is None
    assert s3_uri_to_key("plain/key.txt") == "plain/key.txt"
    assert s3_uri_to_key("s3://bucket") is None
    assert s3_uri_to_key("s3://bucket/path/to/file.txt") == "path/to/file.txt"


def test_get_sample_id_for_result_uses_fallback_order_and_strips():
    run_with_binder = SimpleNamespace(sample_id=None, binder_name=" binder-1 ", form_id="form-1")
    run_with_form = SimpleNamespace(sample_id=None, binder_name=None, form_id=" form-2 ")
    run_empty = SimpleNamespace(sample_id=None, binder_name=None, form_id=None)

    assert get_sample_id_for_result(run_with_binder) == "binder-1"
    assert get_sample_id_for_result(run_with_form) == "form-2"
    assert get_sample_id_for_result(run_empty) is None


def test_bindcraft_helpers_classify_keys_and_build_prefixes(monkeypatch):
    run = WorkflowRun(id=uuid4(), owner_user_id=uuid4(), sample_id="sampleZ")

    assert _classify_bindcraft_output_key(" ") is None
    assert _classify_bindcraft_output_key("folder/") is None
    assert _classify_bindcraft_output_key(f"{run.id}/Accepted/Animation/report.html") == (
        "report",
        "report.html",
    )
    assert _classify_bindcraft_output_key(f"{run.id}/bindcraft/sampleZ_0_output/preview.png") == (
        "snapshot",
        "preview.png",
    )
    assert _classify_bindcraft_output_key(f"{run.id}/ranker/sampleZ_ranked/model.pdb") == (
        "pdb",
        "model.pdb",
    )
    assert _classify_bindcraft_output_key(f"{run.id}/ranker/sampleZ_final_design_stats.csv") == (
        "stats_csv",
        "sampleZ_final_design_stats.csv",
    )

    prefixes = _build_bindcraft_output_listing_prefixes(run)
    assert prefixes == [
        f"{run.id}/ranker/",
        f"{run.id}/Accepted/Animation/",
        f"{run.id}/bindcraft/sampleZ_0_output/Accepted/Animation/",
        f"{run.id}/bindcraft/sampleZ_0_output/",
    ]

    run_without_sample = SimpleNamespace(id=run.id, sample_id=None, binder_name=None, form_id=None)
    assert _build_bindcraft_output_listing_prefixes(run_without_sample) == [
        f"{run.id}/ranker/",
        f"{run.id}/Accepted/Animation/",
    ]

    monkeypatch.setenv("AWS_S3_BUCKET", "test-bucket")
    assert _build_s3_uri("path/to/file.txt") == "s3://test-bucket/path/to/file.txt"
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    assert _build_s3_uri("path/to/file.txt") == "path/to/file.txt"
