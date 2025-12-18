"""Tests for Seqera service."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.workflows import WorkflowLaunchForm
from app.services.seqera import (
    SeqeraConfigurationError,
    SeqeraLaunchResult,
    SeqeraServiceError,
    _get_required_env,
    launch_seqera_workflow,
)


class TestGetRequiredEnv:
    """Tests for _get_required_env helper."""

    def test_get_existing_env_variable(self):
        """Test getting an existing environment variable."""
        result = _get_required_env("SEQERA_API_URL")
        assert result == "https://api.seqera.test"

    def test_get_missing_env_variable(self):
        """Test that missing env variable raises error."""
        with pytest.raises(SeqeraConfigurationError, match="MISSING_VAR"):
            _get_required_env("MISSING_VAR")


class TestLaunchSeqeraWorkflow:
    """Tests for launch_seqera_workflow function."""

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_success_minimal(self, mock_client_class):
        """Test successful workflow launch with minimal parameters."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {
            "workflowId": "wf_test_123",
        }
        mock_response.reason_phrase = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        # Create form
        form = WorkflowLaunchForm(
            pipeline="https://github.com/test/repo",
        )

        # Execute
        result = await launch_seqera_workflow(form)

        # Verify
        assert isinstance(result, SeqeraLaunchResult)
        assert result.workflow_id == "wf_test_123"
        assert result.status == "submitted"

        # Verify API call
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "https://api.seqera.test/workflow/launch" in call_args[0][0]

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_success_with_all_params(self, mock_client_class):
        """Test successful launch with all parameters."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {
            "workflowId": "wf_full_456",
        }
        mock_response.reason_phrase = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(
            pipeline="https://github.com/test/repo",
            revision="main",
            runName="my-custom-run",
            configProfiles=["docker", "test"],
            paramsText="custom_param: value",
        )

        result = await launch_seqera_workflow(form, dataset_id="dataset_789")

        assert result.workflow_id == "wf_full_456"

        # Verify the payload includes dataset
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert "datasetIds" in payload["launch"]
        assert "dataset_789" in payload["launch"]["datasetIds"]

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_includes_default_params(self, mock_client_class):
        """Test that default parameters are included."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {"workflowId": "wf_123"}
        mock_response.reason_phrase = "OK"

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

        await launch_seqera_workflow(form)

        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        params_text = payload["launch"]["paramsText"]

        # Check default params are included
        assert "use_dgxa100: false" in params_text
        assert 'project: "za08"' in params_text
        assert "outdir:" in params_text

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_with_dataset_adds_input_url(self, mock_client_class):
        """Test that providing a dataset ID adds it to launch payload."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {"workflowId": "wf_dataset_999"}
        mock_response.reason_phrase = "OK"

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

        await launch_seqera_workflow(form, dataset_id="ds_abc")

        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        params_text = payload["launch"]["paramsText"]

        assert "input:" in params_text
        assert "ds_abc" in params_text
        assert "samplesheet.csv" in params_text

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_api_error_response(self, mock_client_class):
        """Test handling of API error response."""
        mock_response = AsyncMock()
        mock_response.is_error = True
        mock_response.status_code = 400
        mock_response.text = "Invalid request"
        mock_response.reason_phrase = "Bad Request"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

        with pytest.raises(SeqeraServiceError, match="400"):
            await launch_seqera_workflow(form)

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_missing_workflow_id_in_response(self, mock_client_class):
        """Test error handling when API response lacks workflowId."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {"status": "success"}
        mock_response.reason_phrase = "OK"

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

        with pytest.raises(SeqeraServiceError, match="workflowId"):
            await launch_seqera_workflow(form)

    def test_launch_missing_env_vars(self):
        """Test that missing environment variables raise error."""
        form = WorkflowLaunchForm(pipeline="https://github.com/test/repo")

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SeqeraConfigurationError):
                # This will fail synchronously when trying to get env vars
                import asyncio

                asyncio.run(launch_seqera_workflow(form))

    @patch("app.services.seqera.httpx.AsyncClient")
    async def test_launch_with_custom_params_text(self, mock_client_class):
        """Test launch with custom paramsText."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.json.return_value = {"workflowId": "wf_params_xyz"}
        mock_response.reason_phrase = "OK"

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client

        form = WorkflowLaunchForm(
            pipeline="https://github.com/test/repo",
            paramsText="my_custom_param: 42\nanother_param: test",
        )

        await launch_seqera_workflow(form)

        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        params_text = payload["launch"]["paramsText"]

        # Should contain both default and custom params
        assert "use_dgxa100: false" in params_text  # default
        assert "my_custom_param: 42" in params_text  # custom
        assert "another_param: test" in params_text  # custom
