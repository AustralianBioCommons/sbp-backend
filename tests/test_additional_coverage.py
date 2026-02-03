"""Additional tests to increase coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.routes.workflow.launch import upload_dataset
from app.routes.workflow.placeholders import get_details
from app.schemas.workflows import DatasetUploadRequest
from app.services.bindflow_executor import BindflowConfigurationError, BindflowExecutorError
from app.services.datasets import DatasetUploadResult


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_success(mock_create, mock_upload):
    """Test successful dataset upload."""
    # Mock dataset creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create_result.raw_response = {"id": "dataset_123"}
    mock_create.return_value = mock_create_result

    # Mock upload
    mock_upload_result = DatasetUploadResult(
        success=True,
        dataset_id="dataset_123",
        message="Uploaded",
    )
    mock_upload.return_value = mock_upload_result

    # Create request
    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    # Execute
    response = await upload_dataset(request)

    # Verify
    assert response.success is True
    assert response.datasetId == "dataset_123"
    mock_create.assert_called_once()
    mock_upload.assert_called_once()


async def test_get_details_returns_placeholder():
    """Test that get_details returns proper placeholder data."""
    result = await get_details("run_abc123")

    assert result.id == "run_abc123"
    assert result.status == "UNKNOWN"
    assert result.runName == ""
    assert isinstance(result.configFiles, list)
    assert isinstance(result.params, dict)


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_create_config_error(mock_create, mock_upload):
    """Test dataset upload handles BindflowConfigurationError during creation."""
    # Mock dataset creation to raise error
    mock_create.side_effect = BindflowConfigurationError("Config error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Config error" in str(exc_info.value.detail)


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_create_service_error(mock_create, mock_upload):
    """Test dataset upload handles BindflowExecutorError during creation."""
    mock_create.side_effect = BindflowExecutorError("Service error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Service error" in str(exc_info.value.detail)


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_upload_value_error(mock_create, mock_upload):
    """Test dataset upload handles ValueError during upload."""
    # Mock successful creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create.return_value = mock_create_result

    # Mock upload to raise ValueError
    mock_upload.side_effect = ValueError("Invalid data")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid data" in str(exc_info.value.detail)


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_upload_config_error(mock_create, mock_upload):
    """Test dataset upload handles BindflowConfigurationError during upload."""
    # Mock successful creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create.return_value = mock_create_result

    # Mock upload to raise error
    mock_upload.side_effect = BindflowConfigurationError("Upload config error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Upload config error" in str(exc_info.value.detail)


@patch("app.routes.workflow.launch.upload_dataset_to_seqera")
@patch("app.routes.workflow.launch.create_seqera_dataset")
async def test_upload_dataset_upload_service_error(mock_create, mock_upload):
    """Test dataset upload handles BindflowExecutorError during upload."""
    # Mock successful creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create.return_value = mock_create_result

    # Mock upload to raise error
    mock_upload.side_effect = BindflowExecutorError("Upload service error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Upload service error" in str(exc_info.value.detail)
