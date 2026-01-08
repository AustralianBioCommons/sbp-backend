"""Tests for PDB file upload routes."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.s3 import S3UploadResult


@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


@pytest.fixture
def mock_pdb_file():
    """Create a mock PDB file."""
    content = b"HEADER    TEST PDB FILE\nATOM      1  CA  ALA A   1       0.000   0.000   0.000\nEND\n"
    return BytesIO(content)


@pytest.fixture
def mock_s3_upload_result():
    """Create a mock S3 upload result."""
    return S3UploadResult(
        success=True,
        file_key="input/20260108_120000_test.pdb",
        bucket="test-bucket",
        file_url="s3://test-bucket/input/20260108_120000_test.pdb",
    )


def test_upload_pdb_file_success(client, mock_pdb_file, mock_s3_upload_result):  # pylint: disable=redefined-outer-name
    """Test successful PDB file upload."""
    with patch("app.routes.pdb_upload.upload_file_to_s3", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = mock_s3_upload_result

        response = client.post(
            "/api/workflows/pdb/upload",
            files={"file": ("test.pdb", mock_pdb_file, "chemical/x-pdb")},
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "PDB file uploaded successfully"
        assert data["fileName"] == "test.pdb"
        assert data["fileId"] == "input/20260108_120000_test.pdb"
        assert "s3://" in data["fileUrl"]  # Returns s3:// URI without credentials


def test_upload_pdb_file_invalid_extension(client):
    """Test upload with invalid file extension."""
    content = BytesIO(b"invalid content")
    response = client.post(
        "/api/workflows/pdb/upload",
        files={"file": ("test.txt", content, "text/plain")},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "must have .pdb extension" in response.json()["detail"]


def test_upload_pdb_file_too_large(client):
    """Test upload with file exceeding size limit."""
    # Create a file larger than 10MB
    large_content = BytesIO(b"X" * (11 * 1024 * 1024))
    response = client.post(
        "/api/workflows/pdb/upload",
        files={"file": ("large.pdb", large_content, "chemical/x-pdb")},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "exceeds 10MB limit" in response.json()["detail"]


def test_upload_pdb_file_s3_configuration_error(client, mock_pdb_file):
    """Test upload with S3 configuration error."""
    from app.services.s3 import S3ConfigurationError

    with patch(
        "app.routes.pdb_upload.upload_file_to_s3", new_callable=AsyncMock
    ) as mock_upload:
        mock_upload.side_effect = S3ConfigurationError("AWS credentials not configured")

        response = client.post(
            "/api/workflows/pdb/upload",
            files={"file": ("test.pdb", mock_pdb_file, "chemical/x-pdb")},
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "S3 configuration error" in response.json()["detail"]


def test_upload_pdb_file_s3_service_error(client, mock_pdb_file):
    """Test upload with S3 service error."""
    from app.services.s3 import S3ServiceError

    with patch(
        "app.routes.pdb_upload.upload_file_to_s3", new_callable=AsyncMock
    ) as mock_upload:
        mock_upload.side_effect = S3ServiceError("Upload failed")

        response = client.post(
            "/api/workflows/pdb/upload",
            files={"file": ("test.pdb", mock_pdb_file, "chemical/x-pdb")},
        )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "S3 upload failed" in response.json()["detail"]


def test_upload_pdb_file_no_file(client):
    """Test upload without providing a file."""
    response = client.post("/api/workflows/pdb/upload")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
