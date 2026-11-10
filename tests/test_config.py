"""Tests for application settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ComputeBackendSettings, GlobusSettings, SeqeraSettings


@pytest.fixture
def globus_values() -> dict[str, str]:
    return {
        "client_id": "backend-client-id",
        "client_secret": "backend-client-secret",
        "backend_collection_id": "backend-compute-collection",
        "s3_collection_id": "backend-s3-collection",
        "collection_root": "/backend",
        "input_dir": "/backend/input",
        "output_dir": "/backend/output",
    }


def test_seqera_work_dir_strips_trailing_slashes() -> None:
    settings = SeqeraSettings(
        api_url="https://api.seqera.test",
        access_token="test-token",
        compute_id="test-compute",
        work_space="test-workspace",
        work_dir="/test/work/dir///",
        _env_file=None,
    )

    assert settings.work_dir == "/test/work/dir"


def test_seqera_work_dir_all_slashes_raises() -> None:
    with pytest.raises(ValidationError, match="work_dir must not be empty"):
        SeqeraSettings(
            api_url="https://api.seqera.test",
            access_token="test-token",
            compute_id="test-compute",
            work_space="test-workspace",
            work_dir="///",
            _env_file=None,
        )


def test_globus_settings_rejects_blank_value() -> None:
    with pytest.raises(ValidationError, match="value must not be empty"):
        GlobusSettings(
            client_id="   ",
            client_secret="test-secret",
            gadi_collection_id="test-gadi-collection",
            s3_collection_id="test-s3-collection",
            gadi_collection_root="/test/gadi/root",
            input_dir="/test/input",
            output_dir="/test/output",
            _env_file=None,
        )


@pytest.mark.parametrize("legacy_names", [False, True])
def test_globus_settings_accepts_collection_field_names(
    legacy_names: bool, globus_values: dict[str, str]
) -> None:
    values = globus_values.copy()
    if legacy_names:
        values["gadi_collection_id"] = values.pop("backend_collection_id")
        values["gadi_collection_root"] = values.pop("collection_root")

    settings = GlobusSettings(**values, _env_file=None)

    assert settings.backend_collection_id == globus_values["backend_collection_id"]
    assert settings.collection_root == globus_values["collection_root"]
    assert settings.model_dump() == globus_values


def test_globus_settings_prefers_new_environment_names(
    monkeypatch: pytest.MonkeyPatch, globus_values: dict[str, str]
) -> None:
    for name, value in globus_values.items():
        monkeypatch.setenv(f"GLOBUS_{name.upper()}", value)
    monkeypatch.setenv("GLOBUS_GADI_COLLECTION_ID", "legacy-collection")
    monkeypatch.setenv("GLOBUS_GADI_COLLECTION_ROOT", "/legacy")

    settings = GlobusSettings(_env_file=None)

    assert settings.model_dump() == globus_values


def test_compute_backend_settings_direct_construction_raises() -> None:
    with pytest.raises(TypeError, match=r"created via load\(\)"):
        ComputeBackendSettings()


def test_compute_backend_settings_load_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = ComputeBackendSettings.load("GADI_", env_file=None)

    assert isinstance(settings, ComputeBackendSettings)
    assert settings.globus is None
    assert settings.enable_agent_healthcheck is False
    assert settings.healthcheck_timeout_seconds == 20


@pytest.mark.parametrize("backend_prefix", ["", "GADI_", "SETONIX_"])
@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize("legacy_names", [False, True])
def test_compute_backend_settings_loads_prefixed_globus_settings(
    backend_prefix: str,
    source: str,
    legacy_names: bool,
    globus_values: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy_fields = {
        "backend_collection_id": "gadi_collection_id",
        "collection_root": "gadi_collection_root",
    }
    prefixed_values = {}
    for name, value in globus_values.items():
        legacy_name = legacy_fields.get(name, name)
        monkeypatch.delenv(f"{backend_prefix}GLOBUS_{name.upper()}", raising=False)
        monkeypatch.delenv(f"{backend_prefix}GLOBUS_{legacy_name.upper()}", raising=False)
        env_name = legacy_name if legacy_names else name
        prefixed_values[f"{backend_prefix}GLOBUS_{env_name.upper()}"] = value

    env_file = None
    if source == "environment":
        for name, value in prefixed_values.items():
            monkeypatch.setenv(name, value)
    else:
        dotenv_path = tmp_path / ".env.backend"
        dotenv_path.write_text(
            "\n".join(f"{name}={value}" for name, value in prefixed_values.items()),
            encoding="utf-8",
        )
        env_file = str(dotenv_path)

    settings = ComputeBackendSettings.load(backend_prefix, globus_enabled=True, env_file=env_file)

    assert isinstance(settings.globus, GlobusSettings)
    assert settings.globus.model_dump() == globus_values
