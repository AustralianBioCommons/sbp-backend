"""Tests for cluster_utils.py."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

import importlib

from app.services import cluster_utils


def test_gadi_project_defaults_to_yz52(monkeypatch):
    monkeypatch.delenv("GADI_PROJECT", raising=False)
    reloaded = importlib.reload(cluster_utils)
    try:
        assert reloaded.GADI_PROJECT == "yz52"
    finally:
        importlib.reload(cluster_utils)


def test_gadi_project_reads_env_var(monkeypatch):
    monkeypatch.setenv("GADI_PROJECT", "ab12")
    reloaded = importlib.reload(cluster_utils)
    try:
        assert reloaded.GADI_PROJECT == "ab12"
    finally:
        monkeypatch.delenv("GADI_PROJECT", raising=False)
        importlib.reload(cluster_utils)
