"""Additional tests to increase coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.routes.workflows import get_details, upload_dataset
from app.schemas.workflows import DatasetUploadRequest
from app.services.datasets import DatasetUploadResult, SeqeraConfigurationError, SeqeraServiceError


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
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


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
async def test_upload_dataset_create_config_error(mock_create, mock_upload):
    """Test dataset upload handles SeqeraConfigurationError during creation."""
    # Mock dataset creation to raise error
    mock_create.side_effect = SeqeraConfigurationError("Config error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Config error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
async def test_upload_dataset_create_service_error(mock_create, mock_upload):
    """Test dataset upload handles SeqeraServiceError during creation."""
    mock_create.side_effect = SeqeraServiceError("Service error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Service error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
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


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
async def test_upload_dataset_upload_config_error(mock_create, mock_upload):
    """Test dataset upload handles SeqeraConfigurationError during upload."""
    # Mock successful creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create.return_value = mock_create_result

    # Mock upload to raise error
    mock_upload.side_effect = SeqeraConfigurationError("Upload config error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Upload config error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
async def test_upload_dataset_upload_service_error(mock_create, mock_upload):
    """Test dataset upload handles SeqeraServiceError during upload."""
    # Mock successful creation
    mock_create_result = AsyncMock()
    mock_create_result.dataset_id = "dataset_123"
    mock_create.return_value = mock_create_result

    # Mock upload to raise error
    mock_upload.side_effect = SeqeraServiceError("Upload service error")

    request = DatasetUploadRequest(
        formData={"sample": "test"},
        datasetName="test-dataset",
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(request)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Upload service error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_dataset_to_seqera")
@patch("app.routes.workflows.create_seqera_dataset")
@patch.dict("os.environ", {"AWS_S3_BUCKET": "test-bucket"})
async def test_upload_dataset_with_pdb_file_key(mock_create, mock_upload):
    """Test dataset upload with pdbFileKey replaces file path with S3 URI."""
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

    # Create request with pdbFileKey
    request = DatasetUploadRequest(
        formData={
            "id": "test-id",
            "structure_file": "input/20260128_123456_test.pdb",
            "chains": "A,B",
        },
        datasetName="test-dataset",
        pdbFileKey="input/20260128_123456_test.pdb",
    )

    # Execute
    response = await upload_dataset(request)

    # Verify response
    assert response.success is True
    assert response.datasetId == "dataset_123"

    # Verify that formData was modified correctly
    mock_upload.assert_called_once()
    uploaded_form_data = mock_upload.call_args[0][1]  # Second argument is form_data
    
    # Check that the file path was replaced with S3 URI
    assert uploaded_form_data["structure_file"] == "s3://test-bucket/input/20260128_123456_test.pdb"
    # Check that starting_pdb was also set (required by workflow)
    assert uploaded_form_data["starting_pdb"] == "s3://test-bucket/input/20260128_123456_test.pdb"
    # Other fields should remain unchanged
    assert uploaded_form_data["id"] == "test-id"
    assert uploaded_form_data["chains"] == "A,B"
