"""Tests to boost coverage for proteinfold executor and config modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, mock_open, patch

import httpx
import pytest
import respx
from groovy_parser.parser import parse_groovy_content

from app.schemas.workflows import WorkflowFormData, WorkflowLaunchForm
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
    _post_to_seqera,
    _tool_params,
    launch_proteinfold_workflow,
)
from app.services.seqera import params_to_yaml_text


def _form_data(**extra) -> WorkflowFormData:
    return WorkflowFormData(workflow="single-prediction", tool="colabfold", **extra)


# =============================================================================
# Tests for params_to_yaml_text()
# =============================================================================


def testparams_to_yaml_text_scalars():
    result = params_to_yaml_text({"outdir": "s3://bucket", "use_gpu": True, "batches": 1})
    assert "outdir: s3://bucket" in result
    assert "use_gpu: true" in result
    assert "batches: 1" in result


def testparams_to_yaml_text_nested_dict():
    result = params_to_yaml_text({"tags": {"key1": "val1", "key2": "val2"}})
    assert "tags:" in result
    assert "key1: val1" in result
    assert "key2: val2" in result


def testparams_to_yaml_text_empty():
    assert params_to_yaml_text({}) == ""


# =============================================================================
# Tests for _tool_params()
# =============================================================================


def test_tool_params_empty_form():
    result = _tool_params(_form_data())
    assert result == {}


def test_tool_params_irrelevant_keys():
    result = _tool_params(_form_data(unknown_key="value", another_key=123))
    assert result == {}


def test_tool_params_with_bool():
    result = _tool_params(_form_data(alphafold2_full_dbs=True))
    assert result == {"alphafold2_full_dbs": True}


def test_tool_params_with_int():
    result = _tool_params(_form_data(colabfold_num_recycles=3))
    assert result == {"colabfold_num_recycles": 3}


def test_tool_params_with_str():
    result = _tool_params(_form_data(alphafold2_random_seed="42"))
    assert result == {"alphafold2_random_seed": "42"}


def test_tool_params_none_value_excluded():
    result = _tool_params(_form_data(alphafold2_full_dbs=None, colabfold_num_recycles=5))
    assert "alphafold2_full_dbs" not in result
    assert result["colabfold_num_recycles"] == 5


def test_tool_params_multiple_keys():
    result = _tool_params(
        _form_data(alphafold2_full_dbs=False, colabfold_num_recycles=2, boltz_use_potentials=True)
    )
    assert len(result) == 3


# =============================================================================
# Tests for _build_params_text()
# =============================================================================


def test_build_params_text_no_form_data_no_custom():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, None)
    assert "outdir: s3://bucket/out" in text
    assert "input: https://sheet.url" in text
    assert "mode: alphafold2" in text


def test_build_params_text_with_form_data():
    form_data = _form_data(colabfold_num_recycles=4)
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
    assert "mode: alphafold2" in text


def test_build_params_text_custom_params_strips_trailing():
    custom = "my_param: abc\n\n"
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, custom)
    assert "my_param: abc" in text


def test_build_params_text_empty_form_data_dict():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "boltz", None, None)
    assert "mode: boltz" in text


# =============================================================================
# Tests for _post_to_seqera()
# =============================================================================


@pytest.mark.anyio
async def test_post_to_seqera_success():
    with respx.mock:
        respx.post("https://api.seqera.test/workflow/launch").mock(
            return_value=httpx.Response(
                200, json={"workflowId": "wf_abc123", "status": "submitted"}
            )
        )
        result = await _post_to_seqera(
            "https://api.seqera.test/workflow/launch",
            {"Authorization": "Bearer token"},
            {"launch": {}},
        )

    assert result.workflow_id == "wf_abc123"
    assert result.status == "submitted"


@pytest.mark.anyio
async def test_post_to_seqera_nested_workflow_id():
    """workflowId can be found nested under the data key."""
    with respx.mock:
        respx.post("https://api.test/workflow/launch").mock(
            return_value=httpx.Response(
                200, json={"data": {"workflowId": "wf_nested"}, "status": "running"}
            )
        )
        result = await _post_to_seqera("https://api.test/workflow/launch", {}, {})
    assert result.workflow_id == "wf_nested"


@pytest.mark.anyio
async def test_post_to_seqera_http_error():
    with respx.mock:
        respx.post("https://api.test/workflow/launch").mock(
            return_value=httpx.Response(401, text="Invalid token")
        )
        with pytest.raises(ProteinfoldExecutorError, match="401"):
            await _post_to_seqera("https://api.test/workflow/launch", {}, {})


@pytest.mark.anyio
async def test_post_to_seqera_missing_workflow_id():
    with respx.mock:
        respx.post("https://api.test/workflow/launch").mock(
            return_value=httpx.Response(200, json={"status": "submitted"})
        )
        with pytest.raises(ProteinfoldExecutorError, match="workflowId"):
            await _post_to_seqera("https://api.test/workflow/launch", {}, {})


# =============================================================================
# Tests for launch_proteinfold_workflow()
# =============================================================================


def _make_launch_form(**kwargs) -> WorkflowLaunchForm:
    defaults = {
        "workflow": "single-prediction",
        "tool": "colabfold",
        "runName": "test-run",
        "paramsText": None,
    }
    defaults.update(kwargs)
    return WorkflowLaunchForm(**defaults)


@pytest.fixture
def seqera_env(monkeypatch):
    """Set required Seqera environment variables for launch tests."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("WORK_SPACE", "ws_123")
    monkeypatch.setenv("COMPUTE_ID", "ce_456")
    monkeypatch.setenv("WORK_DIR", "/work/dir")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test_key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_secret")


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_success(seqera_env):
    expected_result = ProteinfoldLaunchResult(
        workflow_id="wf_success", status="submitted", message=None
    )

    with patch(
        "app.services.proteinfold_executor._post_to_seqera",
        new_callable=AsyncMock,
        return_value=expected_result,
    ) as mock_post, patch(
        "app.services.proteinfold_executor.get_proteinfold_config_text",
        return_value="config_text",
    ):
        form = _make_launch_form()
        result = await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            revision="dev",
            output_id="run-output-id",
            mode="alphafold2",
            form_data=None,
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
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
            config_path="/fake/proteinfold.config",
            output_id="run-output-id",
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_missing_output_id(seqera_env):
    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            output_id=None,
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_empty_output_id(seqera_env):
    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            output_id="   ",
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_with_form_data(seqera_env):
    expected_result = ProteinfoldLaunchResult(workflow_id="wf_form", status="submitted")

    with patch(
        "app.services.proteinfold_executor._post_to_seqera",
        new_callable=AsyncMock,
        return_value=expected_result,
    ), patch(
        "app.services.proteinfold_executor.get_proteinfold_config_text",
        return_value="config_text",
    ):
        form = _make_launch_form()
        result = await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            revision="main",
            output_id="run-output-id",
            mode="colabfold",
            form_data=_form_data(colabfold_num_recycles=3, colabfold_use_templates=True),
            user_email="test@example.com",
            full_name="Test_User",
            institute="example.com",
            ip_address="127.0.0.1",
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
    assert "project" in params


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


def test_get_proteinfold_config_profiles_returns_list():
    profiles = get_proteinfold_config_profiles()
    assert isinstance(profiles, list)


def test_get_proteinfold_config_profiles_contains_singularity():
    profiles = get_proteinfold_config_profiles()
    assert "singularity" in profiles


def test_get_proteinfold_config_text_appends_process_block():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            job_id="my-job",
            user_name="user@ex.com",
            timestamp="20240101_120000",
            full_name="Test_User",
            institute="USYD",
            ip_address="1.2.3.4",
        )
    assert "process {" in result
    assert "clusterOptions" in result


def test_get_proteinfold_config_text_contains_job_fields():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            job_id="my-job",
            user_name="user@ex.com",
            timestamp="20240101_120000",
        )
    assert "my-job" in result
    assert "user@ex.com" in result
    assert "20240101_120000" in result


def test_get_proteinfold_config_text_contains_base_config():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            job_id="my-job",
            user_name="user@ex.com",
            timestamp="20240101_120000",
        )
    assert "base_config" in result
