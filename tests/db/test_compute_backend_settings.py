"""Tests for loading settings through a compute backend."""

import pytest

from app.schemas.backend import ComputeBackendName, DataTransferMode, RepositoryTransferMode
from tests.datagen import ComputeBackendFactory


@pytest.mark.parametrize("repository_transfer", list(RepositoryTransferMode))
@pytest.mark.parametrize("data_transfer", list(DataTransferMode))
@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_get_settings_uses_backend_env_prefix(
    repository_transfer, data_transfer, source, monkeypatch, tmp_path
):
    backend = ComputeBackendFactory.build(
        name=ComputeBackendName.GADI,
        env_prefix="CUSTOM_",
        repository_transfer=repository_transfer,
        data_transfer=data_transfer,
    )
    globus_values = {
        "client_id": "custom-client-id",
        "client_secret": "custom-client-secret",
        "backend_collection_id": "custom-backend-collection",
        "s3_collection_id": "custom-s3-collection",
        "collection_root": "/custom",
        "input_dir": "/custom/input",
        "output_dir": "/custom/output",
    }
    values = {
        "CUSTOM_ENABLE_AGENT_HEALTHCHECK": "true",
        "CUSTOM_HEALTHCHECK_TIMEOUT_SECONDS": "73",
        **{f"CUSTOM_GLOBUS_{name.upper()}": value for name, value in globus_values.items()},
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GADI_HEALTHCHECK_TIMEOUT_SECONDS", "99")
    monkeypatch.setenv("HEALTHCHECK_TIMEOUT_SECONDS", "1")

    env_file = None
    if source == "environment":
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    else:
        dotenv_path = tmp_path / ".env.backend"
        dotenv_path.write_text(
            "\n".join(f"{name}={value}" for name, value in values.items()), encoding="utf-8"
        )
        env_file = str(dotenv_path)

    settings = backend.get_settings(env_file=env_file)

    assert settings.enable_agent_healthcheck is True
    assert settings.healthcheck_timeout_seconds == 73
    if (
        repository_transfer == RepositoryTransferMode.GLOBUS
        or data_transfer == DataTransferMode.GLOBUS
    ):
        assert settings.globus is not None
        assert settings.globus.model_dump() == globus_values
    else:
        assert settings.globus is None
