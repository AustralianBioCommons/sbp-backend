"""Additional tests to increase coverage for upload routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException, status

from app.routes.workflows import (
    get_details,
    upload_dataset,
    upload_wisps_dataset_endpoint,
)
from app.schemas.workflows import DatasetUploadRequest, WispsDatasetUploadRequest
from app.services.s3 import S3ConfigurationError, S3ServiceError, S3UploadResult


def _s3_result(
    key: str = "inputs/samplesheets/test.csv", bucket: str = "my-bucket"
) -> S3UploadResult:
    return S3UploadResult(
        success=True,
        file_key=key,
        bucket=bucket,
        file_url=f"s3://{bucket}/{key}",
    )


# =============================================================================
# Tests for upload_dataset (S3-backed)
# =============================================================================


@patch("app.routes.workflows.upload_csv_to_s3")
async def test_upload_dataset_success(mock_upload):
    """Test successful CSV upload to S3."""
    mock_upload.return_value = _s3_result()

    response = await upload_dataset(DatasetUploadRequest(formData={"sample": "test"}))

    assert response.success is True
    assert response.s3Key == "inputs/samplesheets/test.csv"
    assert "s3://" in response.s3Uri
    mock_upload.assert_called_once()


@patch("app.routes.workflows.upload_csv_to_s3")
async def test_upload_dataset_value_error(mock_upload):
    """Test that ValueError (e.g. empty formData) returns 400."""
    mock_upload.side_effect = ValueError("formData cannot be empty")

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(DatasetUploadRequest(formData={"sample": "test"}))

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert "formData cannot be empty" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_csv_to_s3")
async def test_upload_dataset_s3_config_error(mock_upload):
    """Test that S3ConfigurationError returns 500."""
    mock_upload.side_effect = S3ConfigurationError("Missing bucket")

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(DatasetUploadRequest(formData={"sample": "test"}))

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "S3 configuration error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_csv_to_s3")
async def test_upload_dataset_s3_service_error(mock_upload):
    """Test that S3ServiceError returns 502."""
    mock_upload.side_effect = S3ServiceError("Upload failed")

    with pytest.raises(HTTPException) as exc_info:
        await upload_dataset(DatasetUploadRequest(formData={"sample": "test"}))

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "S3 upload failed" in str(exc_info.value.detail)


# =============================================================================
# Tests for get_details
# =============================================================================


async def test_get_details_returns_placeholder():
    """Test that get_details returns proper placeholder data."""
    result = await get_details("run_abc123")

    assert result.id == "run_abc123"
    assert result.status == "UNKNOWN"
    assert result.runName == ""
    assert isinstance(result.configFiles, list)
    assert isinstance(result.params, dict)


# =============================================================================
# Tests for upload_interaction_screening_dataset_endpoint (S3-backed)
# =============================================================================


def _screening_request() -> WispsDatasetUploadRequest:
    return WispsDatasetUploadRequest(
        sequences=[
            {"id": "seq_A", "group": "query"},
            {"id": "seq_B", "group": "target"},
        ],
        runId="run-screen-1",
    )


@patch("app.routes.workflows.upload_wisps_samplesheet_to_s3")
async def test_upload_interaction_screening_success(mock_upload):
    """Test successful interaction screening samplesheet upload to S3."""
    s3_result = _s3_result(key="inputs/samplesheets/run-screen-1.csv")
    mock_upload.return_value = (s3_result, "/data/split/run-screen-1")

    response = await upload_wisps_dataset_endpoint("interaction-screening", _screening_request())

    assert response.success is True
    assert response.s3Key == "inputs/samplesheets/run-screen-1.csv"
    assert response.splitOutputDir == "/data/split/run-screen-1"
    mock_upload.assert_called_once()


@patch("app.routes.workflows.upload_wisps_samplesheet_to_s3")
async def test_upload_interaction_screening_value_error(mock_upload):
    """Test that ValueError returns 400."""
    mock_upload.side_effect = ValueError("sequences cannot be empty")

    with pytest.raises(HTTPException) as exc_info:
        await upload_wisps_dataset_endpoint("interaction-screening", _screening_request())

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


@patch("app.routes.workflows.upload_wisps_samplesheet_to_s3")
async def test_upload_interaction_screening_s3_config_error(mock_upload):
    """Test that S3ConfigurationError returns 500."""
    mock_upload.side_effect = S3ConfigurationError("Missing bucket")

    with pytest.raises(HTTPException) as exc_info:
        await upload_wisps_dataset_endpoint("interaction-screening", _screening_request())

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "S3 configuration error" in str(exc_info.value.detail)


@patch("app.routes.workflows.upload_wisps_samplesheet_to_s3")
async def test_upload_interaction_screening_s3_service_error(mock_upload):
    """Test that S3ServiceError returns 502."""
    mock_upload.side_effect = S3ServiceError("Upload failed")

    with pytest.raises(HTTPException) as exc_info:
        await upload_wisps_dataset_endpoint("interaction-screening", _screening_request())

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "S3 upload failed" in str(exc_info.value.detail)
