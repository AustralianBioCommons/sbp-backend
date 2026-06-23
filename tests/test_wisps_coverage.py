from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, mock_open, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import QueuedJob
from app.schemas.workflows import InteractionScreeningFormData, WorkflowLaunchForm
from app.services.seqera import (
    WorkflowExecutorError,
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from app.services.seqera_errors import SeqeraConfigurationError
from app.services.wisps_config import (
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
    get_wisps_executor_script,
)
from app.services.wisps_executor import (
    _get_required_env,
    _samplesheet_url,
    launch_wisps_workflow,
    prepare_wisps_workflow,
)
from app.services.workflow_config_fetcher import (
    _validate_config_path,
    fetch_workflow_config,
)
from tests.datagen import AppUserFactory, WorkflowFactory, WorkflowRunFactory


@contextmanager
def _mock_wisps_db_context():
    """
    Mock the DB/workflow inputs required for launching a Wisps workflow.
    Very simple mocks for when DB is not the focus of the test.
    """
    workflow = Mock(name="workflow")
    workflow_run = Mock(name="workflow_run")
    workflow_run.workflow = workflow
    db_session = Mock(name="db_session")
    queued_job = Mock(name="queued_job")
    with patch("app.services.wisps_executor.QueuedJob", return_value=queued_job) as queued_job_cls:
        yield db_session, workflow_run, workflow, queued_job_cls, queued_job


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
    with pytest.raises(SeqeraConfigurationError):
        _get_required_env("MY_MISSING_VAR")


def test_samplesheet_url_format():
    url = _samplesheet_url("https://api.test", "ws1", "ds1")
    assert "ws1" in url
    assert "ds1" in url
    assert "samplesheet.csv" in url
    assert "https://api.test" in url


@pytest.mark.anyio
async def test_post_seqera_launch_success():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"workflowId": "wf_abc", "status": "submitted"})
        )
        result = await post_seqera_launch(
            "https://api.test/launch", {"launch": {}}, workflow_label="WISPS"
        )
    assert result.workflow_id == "wf_abc"
    assert result.status == "submitted"


@pytest.mark.anyio
async def test_post_seqera_launch_nested_workflow_id():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"data": {"workflowId": "wf_nested"}})
        )
        result = await post_seqera_launch(
            "https://api.test/launch", {"launch": {}}, workflow_label="WISPS"
        )
    assert result.workflow_id == "wf_nested"


@pytest.mark.anyio
async def test_post_seqera_launch_http_error():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(WorkflowExecutorError, match="401"):
            await post_seqera_launch(
                "https://api.test/launch", {"launch": {}}, workflow_label="WISPS"
            )


@pytest.mark.anyio
async def test_post_seqera_launch_missing_workflow_id():
    with respx.mock:
        respx.post("https://api.test/launch").mock(
            return_value=httpx.Response(200, json={"status": "submitted"})
        )
        with pytest.raises(WorkflowExecutorError, match="workflowId"):
            await post_seqera_launch(
                "https://api.test/launch", {"launch": {}}, workflow_label="WISPS"
            )


@pytest.mark.anyio
async def test_launch_wisps_workflow_success(monkeypatch):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    mock_result = WorkflowLaunchResult(workflow_id="wf_xyz", status="submitted")

    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        patch(
            "app.services.wisps_executor.post_seqera_launch",
            new=AsyncMock(return_value=mock_result),
        ),
        patch("app.services.wisps_executor.get_wisps_config_text", return_value="config_text"),
        patch(
            "app.services.wisps_executor.get_wisps_default_params",
            return_value={"outdir": "s3://out", "input": "https://sheet", "mode": "g1-g2"},
        ),
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
            db_session=db_session,
            workflow_run=workflow_run,
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
async def test_prepare_wisps_workflow_writes_expected_queued_job(
    test_db, persistent_models, monkeypatch
):
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)

    form = WorkflowLaunchForm(workflow="interaction-screening", tool="boltz", runName="queued-run")
    form_data = InteractionScreeningFormData(
        workflow="interaction-screening",
        tool="boltz",
        fastaS3Uri="s3://bucket/seqs.fa",
        splitOutputDir="/tmp/split",
    )

    with (
        patch("app.services.wisps_executor.get_wisps_config_text", return_value="config_text"),
        patch(
            "app.services.wisps_executor.get_wisps_config_profiles", return_value=["singularity"]
        ),
        patch("app.services.wisps_executor.get_wisps_executor_script", return_value="prerun_body"),
    ):
        launch_payload = await prepare_wisps_workflow(
            form=form,
            dataset_id="ds1",
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="nf-core/wisps",
            config_path="/fake/config.nf",
            form_data=form_data,
            revision="dev",
            output_id="output-queued",
            user_email="user@test.com",
            full_name="Test User",
            institute="USYD",
            ip_address="1.2.3.4",
        )

    queued_job = test_db.scalar(
        select(QueuedJob).where(QueuedJob.workflow_run_id == workflow_run.id)
    )
    assert queued_job is not None
    assert queued_job.workflow_id == workflow.id
    assert queued_job.workflow_run_id == workflow_run.id
    # TODO: update to "pending" once we have a job queue
    assert queued_job.status == "submitted"
    assert queued_job.next_attempt_at is not None
    assert queued_job.launch_payload == launch_payload
    assert queued_job.launch_payload["computeEnvId"] == "ce1"
    assert queued_job.launch_payload["runName"] == "queued-run"
    assert queued_job.launch_payload["pipeline"] == "nf-core/wisps"
    assert queued_job.launch_payload["workDir"] == "s3://work"
    assert queued_job.launch_payload["workspaceId"] == "ws1"
    assert queued_job.launch_payload["revision"] == "dev"
    assert queued_job.launch_payload["datasetIds"] == ["ds1"]
    assert queued_job.launch_payload["configProfiles"] == ["singularity"]
    assert queued_job.launch_payload["configText"] == "config_text"
    assert queued_job.launch_payload["preRunScript"] == "prerun_body"
    assert queued_job.launch_payload["resume"] is False
    assert "outdir: s3://my-bucket/output-queued" in queued_job.launch_payload["paramsText"]
    assert (
        "input: https://api.seqera.test/workspaces/ws1/datasets/ds1/v/1/n/samplesheet.csv"
        in queued_job.launch_payload["paramsText"]
    )
    assert "tools: boltz" in queued_job.launch_payload["paramsText"]


@pytest.mark.anyio
async def test_launch_wisps_workflow_with_prerun_script_path(monkeypatch):
    """prerun_script_path is forwarded to get_wisps_executor_script."""
    monkeypatch.setenv("SEQERA_API_URL", "https://api.seqera.test")
    monkeypatch.setenv("SEQERA_ACCESS_TOKEN", "token123")
    monkeypatch.setenv("WORK_SPACE", "ws1")
    monkeypatch.setenv("COMPUTE_ID", "ce1")
    monkeypatch.setenv("WORK_DIR", "s3://work")
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")

    mock_result = WorkflowLaunchResult(workflow_id="wf_prerun", status="submitted")
    prerun_url = "https://raw.githubusercontent.com/org/repo/main/wisps_prerun.sh"

    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        patch(
            "app.services.wisps_executor.post_seqera_launch",
            new=AsyncMock(return_value=mock_result),
        ),
        patch("app.services.wisps_executor.get_wisps_config_text", return_value="config_text"),
        patch(
            "app.services.wisps_executor.get_wisps_default_params",
            return_value={"outdir": "s3://out", "input": "https://sheet", "mode": "g1-g2"},
        ),
        patch(
            "app.services.wisps_executor.get_wisps_executor_script", return_value="prerun_body"
        ) as mock_script,
    ):
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
            db_session=db_session,
            workflow_run=workflow_run,
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
    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError, match="SEQERA_API_URL"),
    ):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            db_session=db_session,
            workflow_run=workflow_run,
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
    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError, match="output identifier"),
    ):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            db_session=db_session,
            workflow_run=workflow_run,
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
    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError),
    ):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            db_session=db_session,
            workflow_run=workflow_run,
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
    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError, match="run name"),
    ):
        await launch_wisps_workflow(
            form=form,
            dataset_id="ds1",
            db_session=db_session,
            workflow_run=workflow_run,
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

    mock_result = WorkflowLaunchResult(workflow_id="wf_tool", status="submitted")

    with (
        _mock_wisps_db_context() as (db_session, workflow_run, *_),
        patch(
            "app.services.wisps_executor.post_seqera_launch",
            new=AsyncMock(return_value=mock_result),
        ),
        patch("app.services.wisps_executor.get_wisps_config_text", return_value="config_text"),
        patch(
            "app.services.wisps_executor.get_wisps_default_params",
            return_value={
                "outdir": "s3://out",
                "input": "https://sheet",
                "mode": "g1-g2",
                "tools": "boltz",
            },
        ),
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
            db_session=db_session,
            workflow_run=workflow_run,
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
