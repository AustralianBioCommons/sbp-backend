"""Tests for Seqera service."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.schemas.workflows import WorkflowLaunchForm
from app.services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
    _get_required_env,
    launch_bindflow_workflow,
)


def test_get_existing_env_variable():
    """Test getting an existing environment variable."""
    result = _get_required_env("SEQERA_API_URL")
    assert result == "https://api.seqera.test"


def test_get_missing_env_variable():
    """Test that missing env variable raises error."""
    with pytest.raises(BindflowConfigurationError, match="MISSING_VAR"):
        _get_required_env("MISSING_VAR")


@pytest.mark.asyncio
@respx.mock
async def test_launch_success_minimal():
    """Test successful workflow launch with minimal parameters."""
    # Mock the Seqera API
    route = respx.post("https://api.seqera.test/workflow/launch").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_test_123"},
        )
    )

    # Create form
    form = WorkflowLaunchForm(tool="BindCraft")

    # Execute
    result = await launch_bindflow_workflow(
        form,
        dataset_id="dataset_min_001",
        pipeline="https://github.com/test/repo",
        output_id="run-out-1",
    )

    # Verify result
    assert isinstance(result, BindflowLaunchResult)
    assert result.workflow_id == "wf_test_123"
    assert result.status == "submitted"

    # Verify API was called once
    assert route.called
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_launch_success_with_all_params():
    """Test successful launch with all parameters."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(
            200,
            json={"workflowId": "wf_full_456"},
        )
    )

    form = WorkflowLaunchForm(
        tool="BindCraft",
        runName="my-custom-run",
        configProfiles=["docker", "test"],
        paramsText="custom_param: value",
    )

    result = await launch_bindflow_workflow(
        form,
        dataset_id="dataset_789",
        pipeline="https://github.com/test/repo",
        revision="main",
        output_id="run-out-2",
    )

    assert result.workflow_id == "wf_full_456"

    # Verify the payload includes dataset
    assert route.called
    request = route.calls.last.request
    # Read the request body and parse JSON
    import json

    payload = json.loads(request.content)
    assert "datasetIds" in payload["launch"]
    assert "dataset_789" in payload["launch"]["datasetIds"]


@pytest.mark.asyncio
@respx.mock
async def test_launch_includes_default_params():
    """Test that default parameters are included."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_123"})
    )

    form = WorkflowLaunchForm(tool="BindCraft")

    await launch_bindflow_workflow(
        form,
        dataset_id="dataset_defaults_001",
        pipeline="https://github.com/test/repo",
        output_id="run-out-3",
    )

    # Check request payload
    request = route.calls.last.request
    import json

    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    # Check default params are included
    assert "use_dgxa100: false" in params_text
    assert 'project: "yz52"' in params_text
    assert "outdir:" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_dataset_adds_input_url():
    """Test that providing a dataset ID adds it to launch payload."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_dataset_999"})
    )

    form = WorkflowLaunchForm(tool="BindCraft")

    await launch_bindflow_workflow(
        form,
        dataset_id="ds_abc",
        pipeline="https://github.com/test/repo",
        output_id="run-out-4",
    )

    # Verify request payload
    request = route.calls.last.request
    import json

    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    assert "input:" in params_text
    assert "ds_abc" in params_text
    assert "samplesheet.csv" in params_text


@pytest.mark.asyncio
@respx.mock
async def test_launch_api_error_response():
    """Test handling of API error response."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(400, text="Invalid request")
    )

    form = WorkflowLaunchForm(tool="BindCraft")

    with pytest.raises(BindflowExecutorError, match="400"):
        await launch_bindflow_workflow(
            form,
            dataset_id="dataset_error_001",
            pipeline="https://github.com/test/repo",
            output_id="run-out-5",
        )


@pytest.mark.asyncio
@respx.mock
async def test_launch_missing_workflow_id_in_response():
    """Test error handling when API response lacks workflowId."""
    respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    form = WorkflowLaunchForm(tool="BindCraft")

    with pytest.raises(BindflowExecutorError, match="workflowId"):
        await launch_bindflow_workflow(
            form,
            dataset_id="dataset_error_002",
            pipeline="https://github.com/test/repo",
            output_id="run-out-6",
        )


def test_launch_missing_env_vars():
    """Test that missing environment variables raise error."""
    form = WorkflowLaunchForm(tool="BindCraft")

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("SEQERA_API_URL", raising=False)
        mp.delenv("SEQERA_ACCESS_TOKEN", raising=False)
        mp.delenv("WORK_SPACE", raising=False)

        with pytest.raises(BindflowConfigurationError):
            import asyncio

            asyncio.run(
                launch_bindflow_workflow(
                    form,
                    dataset_id="dataset_env_001",
                    pipeline="https://github.com/test/repo",
                    output_id="run-out-7",
                )
            )


@pytest.mark.asyncio
@respx.mock
async def test_launch_with_custom_params_text():
    """Test launch with custom paramsText."""
    route = respx.post(url__regex=r".*/workflow/launch.*").mock(
        return_value=httpx.Response(200, json={"workflowId": "wf_params_xyz"})
    )

    form = WorkflowLaunchForm(
        tool="BindCraft",
        paramsText="my_custom_param: 42\nanother_param: test",
    )

    await launch_bindflow_workflow(
        form,
        dataset_id="dataset_params_001",
        pipeline="https://github.com/test/repo",
        output_id="run-out-8",
    )

    # Verify request payload
    request = route.calls.last.request
    import json

    payload = json.loads(request.content)
    params_text = payload["launch"]["paramsText"]

    # Should contain both default and custom params
    assert "use_dgxa100: false" in params_text  # default
    assert "my_custom_param: 42" in params_text  # custom
    assert "another_param: test" in params_text  # custom
