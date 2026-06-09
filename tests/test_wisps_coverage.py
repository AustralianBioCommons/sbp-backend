from __future__ import annotations

from unittest.mock import AsyncMock, mock_open, patch

import httpx
import pytest
import respx

from app.schemas.workflows import InteractionScreeningFormData, WorkflowLaunchForm
from app.services.wisps_config import (
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
    get_wisps_executor_script,
)
from app.services.seqera import params_to_yaml_text
from app.services.wisps_executor import (
    WispsConfigurationError,
    WispsExecutorError,
    WispsLaunchResult,
    _get_required_env,
    _post_to_seqera,
    _samplesheet_url,
    launch_wisps_workflow,
)
from app.services.workflow_config_fetcher import (
    _validate_config_path,
    fetch_workflow_config,
)

# =============================================================================
# Tests for wisps_config.py
# =============================================================================


def test_get_wisps_default_params_required_keys():
    params = get_wisps_default_params(
        out_dir="s3://bucket/out", samplesheet_url="https://api/sheet.csv"
    )
    assert "outdir" in params
    assert "input" in params
    assert "mode" in params


def test_get_wisps_default_params_no_tool():
    params = get_wisps_default_params(
        out_dir="s3://bucket/out", samplesheet_url="https://api/sheet.csv", tool=None
    )
    assert "tools" not in params


def test_get_wisps_default_params_with_tool():
    params = get_wisps_default_params(
        out_dir="s3://bucket/out", samplesheet_url="https://api/sheet.csv", tool="boltz"
    )
    assert params["tools"] == "boltz"


def test_get_wisps_executor_script_header_contains_s3_path():
    script = get_wisps_executor_script(
        fasta_s3_uri="s3://bucket/path.fa",
        split_output_dir="/tmp/seqs",
    )
    assert "bucket/path.fa" in script


def test_get_wisps_executor_script_header_contains_split_output_dir():
    script = get_wisps_executor_script(
        fasta_s3_uri="s3://bucket/path.fa",
        split_output_dir="/tmp/seqs",
    )
    assert "/tmp/seqs" in script


def test_get_wisps_executor_script_header_contains_aws_credentials():
    script = get_wisps_executor_script(
        fasta_s3_uri="s3://bucket/path.fa",
        split_output_dir="/tmp/seqs",
        aws_access_key="KEY",
        aws_secret_key="SECRET",
    )
    assert "KEY" in script
    assert "SECRET" in script


def test_get_wisps_executor_script_header_default_region():
    script = get_wisps_executor_script(
        fasta_s3_uri="s3://bucket/path.fa",
        split_output_dir="/tmp/seqs",
    )
    assert "ap-southeast-2" in script


def test_get_wisps_executor_script_header_custom_region():
    script = get_wisps_executor_script(
        fasta_s3_uri="s3://bucket/path.fa",
        split_output_dir="/tmp/seqs",
        aws_region="us-east-1",
    )
    assert "us-east-1" in script


def test_get_wisps_executor_script_fetches_from_url():
    """When prerun_script_path is set, the body is fetched and appended after the header."""
    fetched_body = "module load singularity\nmodule load nextflow\nexport AWS_ACCESS_KEY_ID\n"
    with patch(
        "app.services.wisps_config.fetch_workflow_config", return_value=fetched_body
    ) as mock_fetch:
        script = get_wisps_executor_script(
            fasta_s3_uri="s3://bucket/path.fa",
            split_output_dir="/tmp/seqs",
            aws_access_key="KEY",
            aws_secret_key="SECRET",
            aws_region="ap-southeast-2",
            prerun_script_path="https://raw.githubusercontent.com/org/repo/main/wisps_prerun.sh",
        )
    mock_fetch.assert_called_once_with(
        "https://raw.githubusercontent.com/org/repo/main/wisps_prerun.sh"
    )
    assert fetched_body in script
    assert "KEY" in script
    assert "bucket/path.fa" in script
    assert "/tmp/seqs" in script


def test_get_wisps_executor_script_no_path_returns_header_only():
    """When prerun_script_path is None, only the variable-assignment header is returned."""
    with patch("app.services.wisps_config.fetch_workflow_config") as mock_fetch:
        script = get_wisps_executor_script(
            fasta_s3_uri="s3://bucket/path.fa",
            split_output_dir="/tmp/seqs",
        )
    mock_fetch.assert_not_called()
    assert "S3_PATH=bucket/path.fa" in script
    assert 'D="/tmp/seqs"' in script


def test_get_wisps_config_profiles_returns_list():
    result = get_wisps_config_profiles()
    assert isinstance(result, list)


def test_get_wisps_config_profiles_contains_singularity():
    result = get_wisps_config_profiles()
    assert "singularity" in result


def test_get_wisps_config_text_appends_process_block():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_wisps_config_text(
            config_file_path="/fake/path.config",
            job_id="my-job",
            username="user@ex.com",
            timestamp="20240101_120000",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
        )
    assert "process {" in result
    assert "clusterOptions" in result


def test_get_wisps_config_text_contains_job_fields():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_wisps_config_text(
            config_file_path="/fake/path.config",
            job_id="my-job",
            username="user@ex.com",
            timestamp="20240101_120000",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
        )
    assert "my-job" in result
    assert "user@ex.com" in result


def test_get_wisps_config_text_contains_base_config():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_wisps_config_text(
            config_file_path="/fake/path.config",
            job_id="my-job",
            username="user@ex.com",
            timestamp="20240101_120000",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
        )
    assert "base_config" in result


# =============================================================================
# Tests for wisps_executor.py
# =============================================================================


def testparams_to_yaml_text_empty():
    assert params_to_yaml_text({}) == ""


def testparams_to_yaml_text_scalars():
    result = params_to_yaml_text(
        {"outdir": "s3://bucket", "input": "https://sheet", "mode": "g1-g2"}
    )
    assert "outdir: s3://bucket" in result
    assert "input: https://sheet" in result
    assert "mode: g1-g2" in result


def test_get_required_env_present(monkeypatch):
    monkeypatch.setenv("MY_VAR", "val")
    assert _get_required_env("MY_VAR") == "val"


def test_get_required_env_missing(monkeypatch):
    monkeypatch.delenv("MY_MISSING_VAR", raising=False)
    with pytest.raises(WispsConfigurationError):
        _get_required_env("MY_MISSING_VAR")


def test_samplesheet_url_format():
    url = _samplesheet_url("https://api.test", "ws1", "ds1")
    assert "ws1" in url
    assert "ds1" in url
    assert "samplesheet.csv" in url
    assert "https://api.test" in url


@pytest.mark.anyio
async def test_post_to_seqera_success():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"workflowId": "wf_abc", "status": "submitted"})
        )
        result = await _post_to_seqera(
            "https://api.test/launch",
            {"Authorization": "Bearer token"},
            {"launch": {}},
        )
    assert result.workflow_id == "wf_abc"
    assert result.status == "submitted"


@pytest.mark.anyio
async def test_post_to_seqera_nested_workflow_id():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"data": {"workflowId": "wf_nested"}})
        )
        result = await _post_to_seqera(
            "https://api.test/launch",
            {"Authorization": "Bearer token"},
            {"launch": {}},
        )
    assert result.workflow_id == "wf_nested"


@pytest.mark.anyio
async def test_post_to_seqera_http_error():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(WispsExecutorError, match="401"):
            await _post_to_seqera(
                "https://api.test/launch",
                {"Authorization": "Bearer token"},
                {"launch": {}},
            )


@pytest.mark.anyio
async def test_post_to_seqera_missing_workflow_id():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"status": "submitted"})
        )
        with pytest.raises(WispsExecutorError, match="workflowId"):
            await _post_to_seqera(
                "https://api.test/launch",
                {"Authorization": "Bearer token"},
                {"launch": {}},
            )


@pytest.mark.anyio
async def test_launch_wisps_workflow_success(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    mock_result = WispsLaunchResult(workflow_id="wf_xyz", status="submitted")

    with patch(
        "app.services.wisps_executor._post_to_seqera", new=AsyncMock(return_value=mock_result)
    ), patch(
        "app.services.wisps_executor.get_wisps_config_text", return_value="config_text"
    ), patch(
        "app.services.wisps_executor.get_wisps_default_params",
        return_value={"outdir": "s3://out", "input": "https://sheet", "mode": "g1-g2"},
    ):
        form = WorkflowLaunchForm(
            workflow="interaction-screening", tool="boltz", runName="test-run"
        )
        form_data = InteractionScreeningFormData(
            workflow="interaction-screening",
            tool="boltz",
            fastaS3Uri="s3://bucket/seqs.fa",
            splitOutputDir="/tmp/split",
        )
        result = await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="output-001",
        )

    assert result.workflow_id == "wf_xyz"


@pytest.mark.anyio
async def test_launch_wisps_workflow_with_prerun_script_path(monkeypatch):
    """prerun_script_path is forwarded to get_wisps_executor_script."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    mock_result = WispsLaunchResult(workflow_id="wf_prerun", status="submitted")
    prerun_url = "https://raw.githubusercontent.com/org/repo/main/wisps_prerun.sh"

    with patch(
        "app.services.wisps_executor._post_to_seqera", new=AsyncMock(return_value=mock_result)
    ), patch(
        "app.services.wisps_executor.get_wisps_config_text", return_value="config_text"
    ), patch(
        "app.services.wisps_executor.get_wisps_default_params",
        return_value={"outdir": "s3://out", "input": "https://sheet", "mode": "g1-g2"},
    ), patch(
        "app.services.wisps_executor.get_wisps_executor_script", return_value="prerun_body"
    ) as mock_script:
        form = WorkflowLaunchForm(
            workflow="interaction-screening", tool="boltz", runName="prerun-run"
        )
        form_data = InteractionScreeningFormData(
            workflow="interaction-screening",
            tool="boltz",
            fastaS3Uri="s3://bucket/seqs.fa",
            splitOutputDir="/tmp/split",
        )
        result = await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="output-003",
            prerun_script_path=prerun_url,
        )

    assert result.workflow_id == "wf_prerun"
    call_kwargs = mock_script.call_args.kwargs
    assert call_kwargs["prerun_script_path"] == prerun_url


@pytest.mark.anyio
async def test_launch_wisps_workflow_missing_env_var(monkeypatch):
    monkeypatch.delenv("SEQERA_API_URL", raising=False)

    form = WorkflowLaunchForm(workflow="interaction-screening", tool="boltz", runName="test-run")
    form_data = InteractionScreeningFormData(
        workflow="interaction-screening",
        tool="boltz",
        fastaS3Uri="s3://bucket/seqs.fa",
        splitOutputDir="/tmp/split",
    )
    with pytest.raises(WispsConfigurationError, match="SEQERA_API_URL"):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="output-001",
        )


@pytest.mark.anyio
async def test_launch_wisps_workflow_missing_output_id(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    form = WorkflowLaunchForm(workflow="interaction-screening", tool="boltz", runName="test-run")
    form_data = InteractionScreeningFormData(
        workflow="interaction-screening",
        tool="boltz",
        fastaS3Uri="s3://bucket/seqs.fa",
        splitOutputDir="/tmp/split",
    )
    with pytest.raises(WispsConfigurationError, match="output identifier"):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id=None,
        )


@pytest.mark.anyio
async def test_launch_wisps_workflow_empty_output_id(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    form = WorkflowLaunchForm(workflow="interaction-screening", tool="boltz", runName="test-run")
    form_data = InteractionScreeningFormData(
        workflow="interaction-screening",
        tool="boltz",
        fastaS3Uri="s3://bucket/seqs.fa",
        splitOutputDir="/tmp/split",
    )
    with pytest.raises(WispsConfigurationError):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="   ",
        )


@pytest.mark.anyio
async def test_launch_wisps_workflow_missing_run_name(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    form = WorkflowLaunchForm(workflow="interaction-screening", tool="boltz", runName=None)
    form_data = InteractionScreeningFormData(
        workflow="interaction-screening",
        tool="boltz",
        fastaS3Uri="s3://bucket/seqs.fa",
        splitOutputDir="/tmp/split",
    )
    with pytest.raises(WispsConfigurationError, match="run name"):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="output-001",
        )


@pytest.mark.anyio
async def test_launch_wisps_workflow_with_tool(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    mock_result = WispsLaunchResult(workflow_id="wf_tool", status="submitted")

    with patch(
        "app.services.wisps_executor._post_to_seqera", new=AsyncMock(return_value=mock_result)
    ), patch(
        "app.services.wisps_executor.get_wisps_config_text", return_value="config_text"
    ), patch(
        "app.services.wisps_executor.get_wisps_default_params",
        return_value={
            "outdir": "s3://out",
            "input": "https://sheet",
            "mode": "g1-g2",
            "tools": "boltz",
        },
    ):
        form = WorkflowLaunchForm(
            workflow="interaction-screening", tool="boltz", runName="test-run-tool"
        )
        form_data = InteractionScreeningFormData(
            workflow="interaction-screening",
            tool="boltz",
            fastaS3Uri="s3://bucket/seqs.fa",
            splitOutputDir="/tmp/split",
        )
        result = await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
            output_id="output-002",
        )

    assert result.workflow_id == "wf_tool"


# =============================================================================
# Tests for workflow_config_fetcher.py
# =============================================================================


def test_validate_config_path_empty_string():
    with pytest.raises(ValueError, match="empty"):
        _validate_config_path("")


def test_validate_config_path_whitespace_only():
    with pytest.raises(ValueError, match="empty"):
        _validate_config_path("   ")


def test_validate_config_path_local_path_ok():
    _validate_config_path("/some/path.config")


def test_validate_config_path_http_no_host():
    with pytest.raises(ValueError):
        _validate_config_path("https://")


def test_validate_config_path_with_token_param():
    with pytest.raises(ValueError, match="token"):
        _validate_config_path("https://host.com/file.config?token=abc")


def test_validate_config_path_valid_url():
    _validate_config_path("https://raw.githubusercontent.com/org/repo/main/file.config")


def test_fetch_workflow_config_local_file(tmp_path):
    config_file = tmp_path / "test.config"
    config_file.write_text("process { executor = 'slurm' }")
    result = fetch_workflow_config(str(config_file))
    assert "process { executor = 'slurm' }" in result


def test_fetch_workflow_config_http():
    with respx.mock:
        respx.get("https://raw.githubusercontent.com/org/repo/main/file.config").mock(
            return_value=httpx.Response(200, text="config text")
        )
        result = fetch_workflow_config(
            "https://raw.githubusercontent.com/org/repo/main/file.config"
        )
    assert result == "config text"


def test_fetch_workflow_config_empty_path():
    with pytest.raises(ValueError):
        fetch_workflow_config("")


def test_fetch_workflow_config_http_error():
    with respx.mock:
        respx.get("https://raw.githubusercontent.com/org/repo/main/file.config").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        with pytest.raises(httpx.HTTPStatusError):
            fetch_workflow_config("https://raw.githubusercontent.com/org/repo/main/file.config")
