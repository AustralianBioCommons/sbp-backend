"""Tests for cluster_utils.py."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

from app.services import cluster_utils


def test_get_gadi_project_returns_settings_value(mock_settings):
    mock_settings.seqera.gadi_project = "ab12"

    assert cluster_utils.get_gadi_project(mock_settings) == "ab12"
