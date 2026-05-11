"""Tests for the bindflow configuration module."""

from __future__ import annotations

from groovy_parser.parser import parse_groovy_content

from app.services.bindflow_config import (
    get_bindflow_config_profiles,
    get_bindflow_config_text,
    get_bindflow_default_params,
    get_bindflow_executor_script,
)


# =============================================================================
# Tests for get_bindflow_default_params()
# =============================================================================


def test_get_bindflow_default_params_contains_outdir():
    params = get_bindflow_default_params("s3://bucket/out")
    assert params["outdir"] == "s3://bucket/out"


def test_get_bindflow_default_params_required_keys():
    params = get_bindflow_default_params("s3://bucket/out")
    for key in ("project", "bindcraft_container", "batches", "publish_dir_mode"):
        assert key in params


def test_get_bindflow_default_params_is_dict():
    assert isinstance(get_bindflow_default_params("s3://out"), dict)


def test_get_bindflow_default_params_project_value():
    params = get_bindflow_default_params("s3://bucket/out")
    assert params["project"] == "yz52"


# =============================================================================
# Tests for get_bindflow_executor_script()
# =============================================================================


def test_get_bindflow_executor_script_injects_credentials():
    script = get_bindflow_executor_script("MYKEY", "MYSECRET", "us-west-2")
    assert "MYKEY" in script
    assert "MYSECRET" in script
    assert "us-west-2" in script


def test_get_bindflow_executor_script_loads_modules():
    script = get_bindflow_executor_script()
    assert "module load singularity" in script
    assert "module load nextflow" in script


def test_get_bindflow_executor_script_exports_aws_vars():
    script = get_bindflow_executor_script()
    assert "export AWS_ACCESS_KEY_ID" in script
    assert "export AWS_SECRET_ACCESS_KEY" in script
    assert "export AWS_REGION" in script


def test_get_bindflow_executor_script_default_region():
    script = get_bindflow_executor_script()
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


def test_get_bindflow_config_text_contains_top_level_blocks():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "singularity {" in text
    assert "process {" in text
    assert "executor {" in text
    assert "trace {" in text


def test_get_bindflow_config_text_singularity_settings():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "enabled = true" in text
    assert "autoMounts = true" in text
    assert "if89/singularity_cache" in text


def test_get_bindflow_config_text_process_executor():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "pbspro" in text


def test_get_bindflow_config_text_interpolates_job_fields():
    text = get_bindflow_config_text("my-run", "alice@example.com", "20260507_090000")
    assert "my-run" in text
    assert "alice@example.com" in text
    assert "20260507_090000" in text


def test_get_bindflow_config_text_contains_bindcraft_withname():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "BINDCRAFT" in text
    assert "gpuvolta" in text
    assert "dgxa100" in text


def test_get_bindflow_config_text_shell_list():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "bash" in text
    assert "pipefail" in text


def test_get_bindflow_config_text_executor_queue_settings():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "queueSize = 300" in text
    assert "pollInterval" in text


def test_get_bindflow_config_text_trace_section():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    assert "trace_timestamp" in text
    assert "gadi-nf-core-trace" in text


def test_get_bindflow_config_text_is_valid_groovy():
    text = get_bindflow_config_text("run-1", "user@example.com", "20260507_120000")
    tree = parse_groovy_content(text)
    assert tree is not None
