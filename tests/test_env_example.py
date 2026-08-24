"""Tests for the checked-in example environment file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from app.config import Settings


def test_env_example_parses_as_valid_settings(monkeypatch) -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env.example"

    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env_path, override=True)

    settings = Settings(_env_file=None)

    assert settings.seqera.compute_id == "compute-env-id"
    assert settings.admin.forbidden_home_url == "http://localhost:3000/"
