"""Tests for the checked-in example environment file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.config import Settings


@pytest.mark.parametrize("native_only", [False, True])
def test_env_example_parses_as_valid_settings(monkeypatch, native_only) -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env.example"

    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env_path, override=True)
    assert not any(key.startswith("GLOBUS_") for key in os.environ)
    if native_only:
        for key in list(os.environ):
            if key.startswith("GADI_GLOBUS_"):
                monkeypatch.delenv(key)

    settings = Settings(_env_file=None)

    assert settings.seqera.compute_id == "compute-env-id"
    assert settings.admin.forbidden_home_url == "http://localhost:3000/"

    from app.main import create_app

    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
