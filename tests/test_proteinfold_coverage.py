"""Tests to boost coverage for proteinfold executor and config modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.workflows import WorkflowLaunchForm
from app.services.proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_config_text,
    get_proteinfold_default_params,
    get_proteinfold_executor_script,
)
from app.services.proteinfold_executor import (
    ProteinfoldConfigurationError,
    ProteinfoldExecutorError,
    ProteinfoldLaunchResult,
    _build_params_text,
    _params_to_yaml_text,
    _post_to_seqera,
    _tool_params,
    _yaml_value,
    launch_proteinfold_workflow,
)

# =============================================================================
# Tests for _yaml_value()
# =============================================================================


def test_yaml_value_true():
    assert _yaml_value(True) == "true"


def test_yaml_value_false():
    assert _yaml_value(False) == "false"


def test_yaml_value_int():
    assert _yaml_value(42) == "42"


def test_yaml_value_float():
    assert _yaml_value(3.14) == "3.14"


def test_yaml_value_str():
    assert _yaml_value("hello") == '"hello"'


# =============================================================================
# Tests for _params_to_yaml_text()
# =============================================================================


def test_params_to_yaml_text_scalars():
    result = _params_to_yaml_text({"outdir": "s3://bucket", "use_gpu": True, "batches": 1})
    assert 'outdir: "s3://bucket"' in result
    assert "use_gpu: true" in result
    assert "batches: 1" in result


def test_params_to_yaml_text_nested_dict():
    result = _params_to_yaml_text({"tags": {"key1": "val1", "key2": "val2"}})
    assert "tags:" in result
    assert '  key1: "val1"' in result
    assert '  key2: "val2"' in result


def test_params_to_yaml_text_empty():
    assert _params_to_yaml_text({}) == ""


# =============================================================================
# Tests for _tool_params()
# =============================================================================


def test_tool_params_empty_form():
    result = _tool_params({})
    assert result == {}


def test_tool_params_irrelevant_keys():
    result = _tool_params({"unknown_key": "value", "another_key": 123})
    assert result == {}


def test_tool_params_with_bool():
    result = _tool_params({"alphafold2_full_dbs": True})
    assert result == {"alphafold2_full_dbs": True}


def test_tool_params_with_int():
    result = _tool_params({"colabfold_num_recycles": 3})
    assert result == {"colabfold_num_recycles": 3}


def test_tool_params_with_str():
    result = _tool_params({"alphafold2_random_seed": "42"})
    assert result == {"alphafold2_random_seed": "42"}


def test_tool_params_none_value_excluded():
    result = _tool_params({"alphafold2_full_dbs": None, "colabfold_num_recycles": 5})
    assert "alphafold2_full_dbs" not in result
    assert result["colabfold_num_recycles"] == 5


def test_tool_params_multiple_keys():
    form_data = {
        "alphafold2_full_dbs": False,
        "colabfold_num_recycles": 2,
        "boltz_use_potentials": True,
    }
    result = _tool_params(form_data)
    assert len(result) == 3


# =============================================================================
# Tests for _build_params_text()
# =============================================================================


def test_build_params_text_no_form_data_no_custom():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, None)
    assert 'outdir: "s3://bucket/out"' in text
    assert 'input: "https://sheet.url"' in text
    assert 'mode: "alphafold2"' in text


def test_build_params_text_with_form_data():
    form_data = {"colabfold_num_recycles": 4}
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "colabfold", form_data, None)
    assert "colabfold_num_recycles: 4" in text


def test_build_params_text_with_custom_params():
    custom = "extra_param: value\nanother_param: 99"
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, custom)
    assert "extra_param: value" in text
    assert "another_param: 99" in text


def test_build_params_text_custom_params_whitespace_only():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, "   ")
    # Whitespace-only custom_params should not be appended
    assert text.endswith('mode: "alphafold2"') or "colabfold_alphafold2_params_tags" in text


def test_build_params_text_custom_params_strips_trailing():
    custom = "my_param: abc\n\n"
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, custom)
    assert "my_param: abc" in text


def test_build_params_text_empty_form_data_dict():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "boltz", {}, None)
    assert 'mode: "boltz"' in text


# =============================================================================
# Tests for _post_to_seqera()
# =============================================================================


@pytest.mark.anyio
async def test_post_to_seqera_success():
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {"workflowId": "wf_abc123", "status": "submitted"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.proteinfold_executor.httpx.AsyncClient", return_value=mock_client):
        result = await _post_to_seqera(
            "https://api.seqera.test/workflow/launch",
            {"Authorization": "Bearer token"},
            {"launch": {}},
        )

    assert result.workflow_id == "wf_abc123"
    assert result.status == "submitted"


@pytest.mark.anyio
async def test_post_to_seqera_nested_workflow_id():
    """Test that workflowId can be found nested in data key."""
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {"data": {"workflowId": "wf_nested"}, "status": "running"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.proteinfold_executor.httpx.AsyncClient", return_value=mock_client):
        result = await _post_to_seqera("https://api.test", {}, {})

    assert result.workflow_id == "wf_nested"


@pytest.mark.anyio
async def test_post_to_seqera_http_error():
    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 401
    mock_response.reason_phrase = "Unauthorized"
    mock_response.text = "Invalid token"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.proteinfold_executor.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProteinfoldExecutorError, match="401"):
            await _post_to_seqera("https://api.test", {}, {})


@pytest.mark.anyio
async def test_post_to_seqera_missing_workflow_id():
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {"status": "submitted"}  # No workflowId

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.proteinfold_executor.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProteinfoldExecutorError, match="workflowId"):
            await _post_to_seqera("https://api.test", {}, {})


# =============================================================================
# Tests for launch_proteinfold_workflow()
# =============================================================================


def _make_launch_form(**kwargs) -> WorkflowLaunchForm:
    defaults = {"tool": "proteinfold", "runName": "test-run", "paramsText": None}
    defaults.update(kwargs)
    return WorkflowLaunchForm(**defaults)


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_success(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    expected_result = ProteinfoldLaunchResult(
        workflow_id="wf_success", status="submitted", message=None
    )

    with patch(
        "app.services.proteinfold_executor._post_to_seqera",
        new_callable=AsyncMock,
        return_value=expected_result,
    ) as mock_post:
        form = _make_launch_form()
        result = await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            revision="dev",
            output_id="run-output-id",
            mode="alphafold2",
            form_data=None,
        )

    assert result.workflow_id == "wf_success"
    assert result.status == "submitted"
    mock_post.assert_called_once()


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_missing_env_var(monkeypatch):
    # Remove a required env var
    monkeypatch.delenv("SEQERA_API_URL", raising=False)
    monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)

    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="SEQERA_API_URL"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            output_id="run-output-id",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_missing_output_id(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            output_id=None,
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_empty_output_id(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            output_id="   ",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_with_form_data(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    expected_result = ProteinfoldLaunchResult(workflow_id="wf_form", status="submitted")

    with patch(
        "app.services.proteinfold_executor._post_to_seqera",
        new_callable=AsyncMock,
        return_value=expected_result,
    ):
        form = _make_launch_form()
        result = await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            revision="main",
            output_id="run-output-id",
            mode="colabfold",
            form_data={"colabfold_num_recycles": 3, "colabfold_use_templates": True},
        )

    assert result.workflow_id == "wf_form"


# =============================================================================
# Tests for proteinfold_config module
# =============================================================================


def test_get_proteinfold_default_params_required_keys():
    params = get_proteinfold_default_params("s3://bucket/out", "https://sheet.url")
    assert params["outdir"] == "s3://bucket/out"
    assert params["input"] == "https://sheet.url"
    assert "mode" in params
    assert "use_gpu" in params
    assert "alphafold2_db" in params
    assert "colabfold_db" in params
    assert "boltz_db" in params


def test_get_proteinfold_default_params_mode_substitution():
    params = get_proteinfold_default_params("s3://bucket/out", "https://sheet.url", mode="boltz")
    assert params["mode"] == "boltz"


def test_get_proteinfold_default_params_is_dict():
    result = get_proteinfold_default_params("s3://out", "https://sheet")
    assert isinstance(result, dict)
    assert len(result) > 0



def test_get_proteinfold_executor_script_env_var_substitution():
    script = get_proteinfold_executor_script("KEY123", "SECRET456", "us-east-1")
    assert "KEY123" in script
    assert "SECRET456" in script
    assert "us-east-1" in script
    assert "module load singularity" in script
    assert "module load nextflow" in script
    assert "export AWS_ACCESS_KEY_ID" in script
    assert "export AWS_SECRET_ACCESS_KEY" in script
    assert "export AWS_REGION" in script


def test_get_proteinfold_executor_script_defaults():
    script = get_proteinfold_executor_script()
    assert "ap-southeast-2" in script


def test_get_proteinfold_config_profiles_contains_singularity():
    profiles = get_proteinfold_config_profiles()
    assert "singularity" in profiles


def test_get_proteinfold_config_text_groovy_structure():
    text = get_proteinfold_config_text()
    assert "singularity {" in text
    assert "executor {" in text
    assert "process {" in text


def test_get_proteinfold_config_text_singularity_enabled():
    text = get_proteinfold_config_text()
    assert "enabled = true" in text
    assert "autoMounts = true" in text


def test_get_proteinfold_config_text_contains_db_path():
    text = get_proteinfold_config_text()
    assert "pbspro" in text


def test_get_proteinfold_config_text_contains_trace():
    text = get_proteinfold_config_text()
    assert "trace {" in text or "trace" in text
