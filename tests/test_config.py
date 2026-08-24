"""Tests for application settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import GlobusSettings, SeqeraSettings


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
