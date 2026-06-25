"""Tests for the bindflow configuration module."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

from unittest.mock import mock_open, patch

import httpx
import pytest
import respx

from app.services.bindflow_config import (
    get_bindflow_config_profiles,
    get_bindflow_config_text,
    get_bindflow_default_params,
)
from app.services.launch_payloads import get_executor_script

_SHEET_URL = "https://api.seqera.test/workspaces/ws1/datasets/ds1/v/1/n/samplesheet.csv"

# =============================================================================
# Tests for get_bindflow_default_params()
# =============================================================================


def test_get_bindflow_default_params_contains_outdir():
    params = get_bindflow_default_params("s3://bucket/out", _SHEET_URL)
    assert params["outdir"] == "s3://bucket/out"


def test_get_bindflow_default_params_contains_input():
    params = get_bindflow_default_params("s3://bucket/out", _SHEET_URL)
    assert params["input"] == _SHEET_URL


def test_get_bindflow_default_params_required_keys():
    params = get_bindflow_default_params("s3://bucket/out", _SHEET_URL)
    for key in ("project", "outdir", "input"):
        assert key in params


def test_get_bindflow_default_params_is_dict():
    assert isinstance(get_bindflow_default_params("s3://out", _SHEET_URL), dict)


def test_get_bindflow_default_params_project_value():
    params = get_bindflow_default_params("s3://bucket/out", _SHEET_URL)
    assert params["project"] == "yz52"


# =============================================================================
# Tests for get_executor_script()
# =============================================================================


def _bindflow_executor_script(
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "ap-southeast-2",
) -> str:
    return get_executor_script(
        prerun_script_path=None,
        module_loads=["singularity", "nextflow"],
        env={
            "AWS_ACCESS_KEY_ID": aws_access_key,
            "AWS_SECRET_ACCESS_KEY": aws_secret_key,
            "AWS_REGION": aws_region,
        },
    )


def test_get_executor_script_injects_credentials():
    script = _bindflow_executor_script("MYKEY", "MYSECRET", "us-west-2")
    assert "MYKEY" in script
    assert "MYSECRET" in script
    assert "us-west-2" in script


def test_get_executor_script_loads_modules():
    script = _bindflow_executor_script()
    assert "module load singularity" in script
    assert "module load nextflow" in script


def test_get_executor_script_exports_aws_vars():
    script = _bindflow_executor_script()
    assert "export AWS_ACCESS_KEY_ID" in script
    assert "export AWS_SECRET_ACCESS_KEY" in script
    assert "export AWS_REGION" in script


def test_get_executor_script_default_region():
    script = _bindflow_executor_script()
    assert "ap-southeast-2" in script


# =============================================================================
# Tests for get_bindflow_config_profiles()
# =============================================================================


def test_get_bindflow_config_profiles_returns_list():
    assert isinstance(get_bindflow_config_profiles(), list)


def test_get_bindflow_config_profiles_contains_singularity_and_gadi():
    profiles = get_bindflow_config_profiles()
    assert "singularity" in profiles
    assert "gadi" in profiles


# =============================================================================
# Tests for get_bindflow_config_text()
# =============================================================================


def test_get_bindflow_config_text_includes_base_config():
    with patch("builtins.open", mock_open(read_data="base_config_content")):
        result = get_bindflow_config_text(
            "/fake/bindflow.config",
            job_id="run-1",
            username="user@example.com",
            timestamp="20260507_120000",
        )
    assert "base_config_content" in result


def test_get_bindflow_config_text_appends_process_block():
    with patch("builtins.open", mock_open(read_data="")):
        result = get_bindflow_config_text(
            "/fake/bindflow.config",
            job_id="run-1",
            username="user@example.com",
            timestamp="20260507_120000",
        )
    assert "process {" in result
    assert "clusterOptions" in result


def test_get_bindflow_config_text_interpolates_job_fields():
    with patch("builtins.open", mock_open(read_data="")):
        result = get_bindflow_config_text(
            "/fake/bindflow.config",
            job_id="my-run",
            username="alice@example.com",
            timestamp="20260507_090000",
        )
    assert "my-run" in result
    assert "alice@example.com" in result
    assert "20260507_090000" in result


def test_get_bindflow_config_text_optional_fields():
    with patch("builtins.open", mock_open(read_data="")):
        result = get_bindflow_config_text(
            "/fake/bindflow.config",
            job_id="run-1",
            username="user@example.com",
            timestamp="ts",
            full_name="Alice Smith",
            institute="BioCommons",
            ip_address="1.2.3.4",
        )
    assert "Alice Smith" in result
    assert "BioCommons" in result
    assert "1.2.3.4" in result


def test_get_bindflow_config_text_url_fetching():
    with respx.mock:
        respx.get("https://raw.githubusercontent.com/org/repo/main/bindflow.config").mock(
            return_value=httpx.Response(200, text="remote_base_config")
        )
        result = get_bindflow_config_text(
            "https://raw.githubusercontent.com/org/repo/main/bindflow.config",
            job_id="run-url",
            username="user@example.com",
            timestamp="20260507_120000",
        )
    assert "remote_base_config" in result
    assert "clusterOptions" in result


def test_get_bindflow_config_text_url_error_raises():
    with respx.mock:
        respx.get("https://raw.githubusercontent.com/org/repo/main/bindflow.config").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        with pytest.raises(httpx.HTTPStatusError):
            get_bindflow_config_text(
                "https://raw.githubusercontent.com/org/repo/main/bindflow.config",
                job_id="run-1",
                username="user@example.com",
                timestamp="ts",
            )
