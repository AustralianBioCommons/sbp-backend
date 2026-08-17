"""Tests for application settings."""

from __future__ import annotations

from app.config import SeqeraSettings


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
