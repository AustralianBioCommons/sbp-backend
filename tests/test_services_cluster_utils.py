"""Tests for cluster_utils.py."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

import base64
import importlib

from app.services import cluster_utils
from app.services.cluster_utils import encode_ip


def test_encode_ip_returns_base64_of_ip_string():
    assert encode_ip("1.2.3.4") == base64.b64encode(b"1.2.3.4").decode()


def test_encode_ip_empty_string():
    assert encode_ip("") == ""


def test_encode_ip_ipv6_address():
    ip = "2001:db8::1"
    assert encode_ip(ip) == base64.b64encode(ip.encode()).decode()


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
