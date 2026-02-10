"""Tests for Seqera service helpers."""

from __future__ import annotations

from app.services.seqera import _extract_workflow_type


def test_extract_workflow_type():
    """Test workflow type extraction heuristics."""
    assert _extract_workflow_type({"pipeline": "bindflow"}) == "BindCraft"
    assert _extract_workflow_type({"projectName": "Hello", "pipeline": ""}) == "Hello World"
    assert _extract_workflow_type({"projectName": "Custom", "pipeline": ""}) == "Custom"
