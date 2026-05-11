"""Tests to boost coverage for proteinfold executor and config modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from groovy_parser.parser import parse_groovy_content

from app.schemas.workflows import WorkflowLaunchForm
from app.services._nf_config import (
    GADI_TRACE_SECTION,
    Raw,
    _block,
    _serialize,
    build_nf_config,
)
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
    launch_proteinfold_workflow,
)

# =============================================================================
# Tests for _params_to_yaml_text()
# =============================================================================


def test_params_to_yaml_text_scalars():
    result = _params_to_yaml_text({"outdir": "s3://bucket", "use_gpu": True, "batches": 1})
    assert "outdir: s3://bucket" in result
    assert "use_gpu: true" in result
    assert "batches: 1" in result


def test_params_to_yaml_text_nested_dict():
    result = _params_to_yaml_text({"tags": {"key1": "val1", "key2": "val2"}})
    assert "tags:" in result
    assert "key1: val1" in result
    assert "key2: val2" in result


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
    assert "outdir: s3://bucket/out" in text
    assert "input: https://sheet.url" in text
    assert "mode: alphafold2" in text


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
    assert "mode: alphafold2" in text


def test_build_params_text_custom_params_strips_trailing():
    custom = "my_param: abc\n\n"
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "alphafold2", None, custom)
    assert "my_param: abc" in text


def test_build_params_text_empty_form_data_dict():
    text = _build_params_text("s3://bucket/out", "https://sheet.url", "boltz", {}, None)
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
    defaults = {"tool": "proteinfold", "runName": "test-run", "paramsText": None}
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
async def test_launch_proteinfold_workflow_missing_output_id(seqera_env):
    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            output_id=None,
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_empty_output_id(seqera_env):
    form = _make_launch_form()
    with pytest.raises(ProteinfoldConfigurationError, match="output identifier"):
        await launch_proteinfold_workflow(
            form,
            "dataset_abc",
            pipeline="https://github.com/nf-core/proteinfold",
            output_id="   ",
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_with_form_data(seqera_env):
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
# Tests for _nf_config builder module
# =============================================================================


def test_raw_is_emitted_verbatim():
    assert _serialize(Raw("256.GB")) == "256.GB"


def test_raw_with_closure():
    expr = "{ task.memory < 128.GB ? 'normalbw' : 'normal' }"
    assert _serialize(Raw(expr)) == expr


def test_serialize_bool_true():
    assert _serialize(True) == "true"


def test_serialize_bool_false():
    assert _serialize(False) == "false"


def test_serialize_int():
    assert _serialize(300) == "300"


def test_serialize_float():
    assert _serialize(1.5) == "1.5"


def test_serialize_simple_string_single_quotes():
    assert _serialize("pbspro") == "'pbspro'"


def test_serialize_string_with_single_quote_uses_double():
    assert _serialize("it's here") == '"it\'s here"'


def test_serialize_list():
    result = _serialize(["bash", "-C", "-e"])
    assert result == "['bash', '-C', '-e']"


def test_serialize_dict_produces_groovy_map():
    result = _serialize({"key1": "val1", "key2": "val2"}, depth=1)
    assert '"key1": \'val1\'' in result
    assert '"key2": \'val2\'' in result
    assert result.startswith("[")
    assert result.endswith("]")


def test_serialize_unsupported_type_raises():
    with pytest.raises(TypeError, match="Cannot serialize"):
        _serialize(object())


def test_block_simple():
    result = _block("executor", {"queueSize": 300, "pollInterval": "5 min"})
    assert result.startswith("executor {")
    assert "queueSize = 300" in result
    assert "pollInterval = '5 min'" in result
    assert result.strip().endswith("}")


def test_block_with_nested_withname():
    result = _block(
        "process",
        {
            "executor": "pbspro",
            "withName: 'JOB'": {"memory": Raw("256.GB")},
        },
    )
    assert "withName: 'JOB' {" in result
    assert "memory = 256.GB" in result


def test_block_with_nested_withlabel():
    result = _block(
        "process",
        {"withLabel: 'process_gpu'": {"cpus": 12, "gpus": 1}},
    )
    assert "withLabel: 'process_gpu' {" in result
    assert "cpus = 12" in result
    assert "gpus = 1" in result


def test_block_depth_indentation():
    result = _block("inner", {"key": "val"}, depth=1)
    assert result.startswith("    inner {")
    assert "        key = 'val'" in result


def test_build_nf_config_joins_sections_with_blank_line():
    result = build_nf_config(
        ("singularity", {"enabled": True}),
        ("executor", {"queueSize": 1}),
    )
    assert "singularity {" in result
    assert "executor {" in result
    assert "\n\n" in result


def test_build_nf_config_raw_string_section():
    result = build_nf_config("// a comment", ("trace", {"enabled": True}))
    assert "// a comment" in result
    assert "trace {" in result


def test_gadi_trace_section_contains_expected_fields():
    assert "def trace_timestamp" in GADI_TRACE_SECTION
    assert "trace {" in GADI_TRACE_SECTION
    assert "enabled = true" in GADI_TRACE_SECTION
    assert "${trace_timestamp}" in GADI_TRACE_SECTION


def test_build_nf_config_produces_valid_groovy():
    config = build_nf_config(
        ("params", {"use_gpu": True, "db": "/some/path"}),
        ("singularity", {"enabled": True, "autoMounts": True}),
        ("executor", {"queueSize": 300}),
    )
    tree = parse_groovy_content(config)
    assert tree is not None


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


def test_get_proteinfold_config_profiles_returns_list():
    profiles = get_proteinfold_config_profiles()
    assert isinstance(profiles, list)


def test_get_proteinfold_config_profiles_contains_singularity():
    profiles = get_proteinfold_config_profiles()
    assert "singularity" in profiles


def test_get_proteinfold_config_text_groovy_structure():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "params {" in text
    assert "singularity {" in text
    assert "executor {" in text
    assert "process {" in text


def test_get_proteinfold_config_text_singularity_enabled():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "enabled = true" in text
    assert "autoMounts = true" in text


def test_get_proteinfold_config_text_contains_pbspro():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "pbspro" in text


def test_get_proteinfold_config_text_contains_trace():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "trace {" in text


def test_get_proteinfold_config_text_interpolates_job_fields():
    text = get_proteinfold_config_text("my-job", "alice", "20260507_120000")
    assert "my-job" in text
    assert "alice" in text
    assert "20260507_120000" in text


def test_get_proteinfold_config_text_contains_withname_block():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "MMSEQS_COLABFOLDSEARCH" in text
    assert "256.GB" in text


def test_get_proteinfold_config_text_contains_withlabel_block():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    assert "process_gpu" in text
    assert "gpuvolta" in text


def test_get_proteinfold_config_text_is_valid_groovy():
    text = get_proteinfold_config_text("job-1", "user-1", "20260507_150000")
    tree = parse_groovy_content(text)
    assert tree is not None
