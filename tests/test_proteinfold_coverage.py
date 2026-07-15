"""Tests to boost coverage for proteinfold executor and config modules."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, mock_open, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import QueuedJob
from app.schemas.workflows import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from app.services.launch_payloads import get_executor_script
from app.services.proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_config_text,
    get_proteinfold_default_params,
)
from app.services.proteinfold_executor import (
    _build_params_text,
    _tool_params,
    launch_proteinfold_workflow,
    prepare_proteinfold_workflow,
)
from app.services.seqera import (
    WorkflowExecutorError,
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from app.services.seqera_errors import SeqeraConfigurationError
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory

_USER_DETAILS = WorkflowUserDetails(
    user_email="user@ex.com",
    full_name="Test_User",
    institute="USYD",
    ip_address="1.2.3.4",
)


def _form_data(**extra) -> WorkflowFormData:
    return WorkflowFormData(workflow="single-prediction", tool="colabfold", **extra)


def _queued_proteinfold_job(
    *,
    params_text: str | None = None,
    prerun_script_path: str | None = None,
) -> QueuedJob:
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(
        name="single-prediction",
        prerun_script_path=prerun_script_path,
    )
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)
    launch_payload = {
        "computeEnvId": "ce_456",
        "runName": "test-run",
        "pipeline": "https://github.com/nf-core/proteinfold",
        "workDir": "/work/dir",
        "workspaceId": "ws_123",
        "revision": "dev",
        "paramsText": params_text
        or (
            "outdir: s3://my-bucket/run-output-id\n"
            "input: s3://my-bucket/inputs/samplesheets/test.csv\n"
            "mode: alphafold2"
        ),
        "configProfiles": ["singularity"],
        "configText": "config_text",
        "resume": False,
    }
    return QueuedJobFactory.create_sync(
        workflow=workflow,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        status="pending",
    )


@contextmanager
def _mock_proteinfold_db_context():
    workflow = Mock(name="workflow")
    workflow_run = Mock(name="workflow_run")
    workflow_run.workflow = workflow
    db_session = Mock(name="db_session")
    queued_job = Mock(name="queued_job")
    with patch(
        "app.services.proteinfold_executor.QueuedJob", return_value=queued_job
    ) as queued_job_cls:
        yield db_session, workflow_run, workflow, queued_job_cls, queued_job


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
# Tests for post_seqera_launch()
# =============================================================================


@pytest.mark.anyio
async def test_post_seqera_launch_success():
    with respx.mock:
        respx.post(url__regex=r"https://api\.seqera\.test/workflow/launch.*").mock(
            return_value=httpx.Response(
                200, json={"workflowId": "wf_abc123", "status": "submitted"}
            )
        )
        result = await post_seqera_launch({"launch": {}}, workflow_label="Proteinfold")

    assert result.workflow_id == "wf_abc123"
    assert result.status == "submitted"


@pytest.mark.anyio
async def test_post_seqera_launch_nested_workflow_id():
    """workflowId can be found nested under the data key."""
    with respx.mock:
        respx.post(url__regex=r"https://api\.seqera\.test/workflow/launch.*").mock(
            return_value=httpx.Response(
                200, json={"data": {"workflowId": "wf_nested"}, "status": "running"}
            )
        )
        result = await post_seqera_launch({}, workflow_label="Proteinfold")
    assert result.workflow_id == "wf_nested"


@pytest.mark.anyio
async def test_post_seqera_launch_http_error():
    with respx.mock:
        respx.post(url__regex=r"https://api\.seqera\.test/workflow/launch.*").mock(
            return_value=httpx.Response(401, text="Invalid token")
        )
        with pytest.raises(WorkflowExecutorError, match="401"):
            await post_seqera_launch({}, workflow_label="Proteinfold")


@pytest.mark.anyio
async def test_post_seqera_launch_missing_workflow_id():
    with respx.mock:
        respx.post(url__regex=r"https://api\.seqera\.test/workflow/launch.*").mock(
            return_value=httpx.Response(200, json={"status": "submitted"})
        )
        with pytest.raises(WorkflowExecutorError, match="workflowId"):
            await post_seqera_launch({}, workflow_label="Proteinfold")


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
async def test_launch_proteinfold_workflow_success(seqera_env, persistent_models):
    expected_result = WorkflowLaunchResult(
        workflow_id="wf_success", status="submitted", message=None
    )

    with (
        patch(
            "app.services.proteinfold_executor.post_seqera_launch",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_post,
    ):
        result = await launch_proteinfold_workflow(queued_job=_queued_proteinfold_job())

    assert result.workflow_id == "wf_success"
    assert result.status == "submitted"
    mock_post.assert_called_once()
    posted_payload = mock_post.call_args.args[0]["launch"]
    assert "module load singularity" in posted_payload["preRunScript"]
    assert "module load nextflow" in posted_payload["preRunScript"]
    assert "export AWS_ACCESS_KEY_ID" in posted_payload["preRunScript"]


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_injects_prerun_script_at_launch(
    seqera_env, persistent_models
):
    expected_result = WorkflowLaunchResult(workflow_id="wf_prerun", status="submitted")

    with (
        patch(
            "app.services.proteinfold_executor.post_seqera_launch",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_post,
        patch(
            "app.services.proteinfold_executor.get_executor_script",
            return_value="prerun_body",
        ) as mock_script,
    ):
        result = await launch_proteinfold_workflow(
            queued_job=_queued_proteinfold_job(prerun_script_path="/some/prerun.sh")
        )

    assert result.workflow_id == "wf_prerun"
    posted_payload = mock_post.call_args.args[0]["launch"]
    assert posted_payload["preRunScript"] == "prerun_body"
    assert mock_script.call_args.kwargs["prerun_script_path"] == "/some/prerun.sh"


@pytest.mark.anyio
async def test_prepare_proteinfold_workflow_writes_expected_queued_job(
    test_db, persistent_models, seqera_env
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    workflow_run = WorkflowRunFactory.create_sync(workflow=workflow, owner=user)

    form = _make_launch_form(runName="queued-proteinfold-run")
    form_data = _form_data(colabfold_num_recycles=3, colabfold_use_templates=True)

    with (
        patch(
            "app.services.proteinfold_executor.get_proteinfold_config_text",
            return_value="config_text",
        ),
        patch(
            "app.services.proteinfold_executor.get_proteinfold_config_profiles",
            return_value=["singularity"],
        ),
    ):
        prepared_job = await prepare_proteinfold_workflow(
            form=form,
            s3_input_key="inputs/samplesheets/test.csv",
            db_session=test_db,
            workflow_run=workflow_run,
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            revision="main",
            output_id="run-output-id",
            mode="colabfold",
            form_data=form_data,
            user_details=_USER_DETAILS.model_copy(
                update={
                    "user_email": "test@example.com",
                    "institute": "example.com",
                    "ip_address": "127.0.0.1",
                }
            ),
        )

    queued_job = test_db.scalar(
        select(QueuedJob).where(QueuedJob.workflow_run_id == workflow_run.id)
    )
    assert queued_job is not None
    assert queued_job.workflow_id == workflow.id
    assert queued_job.workflow_run_id == workflow_run.id
    assert queued_job.status == "pending"
    assert queued_job.next_attempt_at is not None
    assert queued_job.id == prepared_job.id
    assert queued_job.launch_payload["computeEnvId"] == "ce_456"
    assert queued_job.launch_payload["runName"] == "queued-proteinfold-run"
    assert queued_job.launch_payload["pipeline"] == "https://github.com/nf-core/proteinfold"
    assert queued_job.launch_payload["workDir"] == "/work/dir"
    assert queued_job.launch_payload["workspaceId"] == "ws_123"
    assert queued_job.launch_payload["revision"] == "main"
    assert queued_job.launch_payload["configProfiles"] == ["singularity"]
    assert queued_job.launch_payload["configText"] == "config_text"
    assert "preRunScript" not in queued_job.launch_payload
    assert queued_job.launch_payload["resume"] is False
    assert "outdir: s3://my-bucket/run-output-id" in queued_job.launch_payload["paramsText"]
    assert (
        "input: s3://my-bucket/inputs/samplesheets/test.csv"
        in queued_job.launch_payload["paramsText"]
    )
    assert "mode: colabfold" in queued_job.launch_payload["paramsText"]
    assert "colabfold_num_recycles: 3" in queued_job.launch_payload["paramsText"]
    assert "colabfold_use_templates: true" in queued_job.launch_payload["paramsText"]


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_missing_env_var(monkeypatch, persistent_models):
    # Remove a required env var
    monkeypatch.delenv("SEQERA_API_URL", raising=False)
    monkeypatch.delenv("SEQERA_ACCESS_TOKEN", raising=False)

    with (pytest.raises(SeqeraConfigurationError, match="SEQERA_API_URL"),):
        await launch_proteinfold_workflow(queued_job=_queued_proteinfold_job())


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_missing_output_id(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteinfold_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError, match="output identifier"),
    ):
        await prepare_proteinfold_workflow(
            form=form,
            s3_input_key="dataset_abc",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            output_id=None,
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_empty_output_id(seqera_env):
    form = _make_launch_form()
    with (
        _mock_proteinfold_db_context() as (db_session, workflow_run, *_),
        pytest.raises(SeqeraConfigurationError, match="output identifier"),
    ):
        await prepare_proteinfold_workflow(
            form=form,
            s3_input_key="dataset_abc",
            db_session=db_session,
            workflow_run=workflow_run,
            pipeline="https://github.com/nf-core/proteinfold",
            config_path="/fake/proteinfold.config",
            output_id="   ",
            user_details=_USER_DETAILS,
        )


@pytest.mark.anyio
async def test_launch_proteinfold_workflow_with_form_data(seqera_env, persistent_models):
    expected_result = WorkflowLaunchResult(workflow_id="wf_form", status="submitted")

    with (
        patch(
            "app.services.proteinfold_executor.post_seqera_launch",
            new_callable=AsyncMock,
            return_value=expected_result,
        ),
    ):
        result = await launch_proteinfold_workflow(
            queued_job=_queued_proteinfold_job(
                params_text=(
                    "outdir: s3://my-bucket/run-output-id\n"
                    "input: s3://my-bucket/inputs/samplesheets/test.csv\n"
                    "mode: colabfold\n"
                    "colabfold_num_recycles: 3\n"
                    "colabfold_use_templates: true"
                )
            )
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


def test_get_executor_script_env_var_substitution():
    script = get_executor_script(
        prerun_script_path=None,
        module_loads=["singularity", "nextflow"],
        env={
            "AWS_ACCESS_KEY_ID": "KEY123",
            "AWS_SECRET_ACCESS_KEY": "SECRET456",
            "AWS_REGION": "us-east-1",
        },
    )
    assert "KEY123" in script
    assert "SECRET456" in script
    assert "us-east-1" in script
    assert "module load singularity" in script
    assert "module load nextflow" in script
    assert "export AWS_ACCESS_KEY_ID" in script
    assert "export AWS_SECRET_ACCESS_KEY" in script
    assert "export AWS_REGION" in script


def test_get_executor_script_defaults():
    script = get_executor_script(
        prerun_script_path=None,
        env={"AWS_REGION": "ap-southeast-2"},
    )
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
            email=_USER_DETAILS.user_email,
            ip_address=_USER_DETAILS.ip_address,
        )
    assert "process {" in result
    assert "clusterOptions" in result


def test_get_proteinfold_config_text_contains_email_and_encoded_ip():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            email=_USER_DETAILS.user_email,
            ip_address=_USER_DETAILS.ip_address,
        )
    assert "user@ex.com" in result
    assert "MS4yLjMuNA==" in result


def test_get_proteinfold_config_text_without_ip_uses_email_only():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            email=_USER_DETAILS.user_email,
        )
    assert "-A user@ex.com" in result
    assert ":" not in result.split("clusterOptions = ")[1]


def test_get_proteinfold_config_text_contains_base_config():
    with patch("builtins.open", mock_open(read_data="base_config")):
        result = get_proteinfold_config_text(
            "/fake/proteinfold.config",
            email=_USER_DETAILS.user_email,
            ip_address=_USER_DETAILS.ip_address,
        )
    assert "base_config" in result
