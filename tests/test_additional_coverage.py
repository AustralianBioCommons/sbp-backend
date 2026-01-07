"""Additional tests to increase coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.routes.workflows import get_details, upload_dataset
from app.schemas.workflows import DatasetUploadRequest
from app.services.datasets import DatasetUploadResult


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
