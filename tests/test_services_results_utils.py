"""Tests for result-specific formatting helpers."""

from __future__ import annotations

from app.services.results_utils import format_log_entries


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
