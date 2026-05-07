"""Tests for FASTA file upload routes."""

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
    from uuid import UUID

    from app.routes.dependencies import get_current_user_id

    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: UUID(
        "11111111-1111-1111-1111-111111111111"
    )
    return TestClient(app)


@pytest.fixture
def mock_s3_upload_result():
    return S3UploadResult(
        success=True,
        file_key="input/20260421_120000_single_prediction.fasta",
        bucket="test-bucket",
        file_url="s3://test-bucket/input/20260421_120000_single_prediction.fasta",
    )


def test_upload_fasta_file_success(client, mock_s3_upload_result):
    with (
        patch("app.routes.fasta_upload.upload_file_to_s3", new_callable=AsyncMock) as mock_upload,
        patch(
            "app.routes.fasta_upload.generate_presigned_url",
            new_callable=AsyncMock,
            return_value="https://signed.example/input/single_prediction.fasta",
        ) as mock_presign,
    ):
        mock_upload.return_value = mock_s3_upload_result
        response = client.post(
            "/api/workflows/fasta/upload",
            files={"file": ("single_prediction.fasta", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
            headers={"Authorization": "Bearer testtoken"},
        )
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["success"] is True
    assert data["message"] == "FASTA file uploaded successfully"
    assert data["fileId"] == "input/20260421_120000_single_prediction.fasta"
    assert data["presignedUrl"] == "https://signed.example/input/single_prediction.fasta"
    mock_presign.assert_awaited_once_with(
        "input/20260421_120000_single_prediction.fasta",
        response_content_type="text/plain",
        response_content_disposition="inline",
    )


def test_upload_fasta_file_rejects_invalid_extension(client):
    response = client.post(
        "/api/workflows/fasta/upload",
        files={"file": ("single_prediction.txt", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert ".fasta" in response.json()["detail"]


def test_upload_fasta_file_rejects_non_fasta_content(client):
    response = client.post(
        "/api/workflows/fasta/upload",
        files={"file": ("single_prediction.fasta", BytesIO(b"ACDEF\n"), "text/plain")},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "must start with" in response.json()["detail"]



def test_upload_fasta_no_filename(client):
    """Empty filename should return a 4xx error."""
    response = client.post(
        "/api/workflows/fasta/upload",
        files={"file": ("", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert response.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


def test_upload_fasta_s3_configuration_error(client):
    """S3ConfigurationError should map to 500."""
    from app.services.s3 import S3ConfigurationError

    with patch(
        "app.routes.fasta_upload.upload_file_to_s3",
        new_callable=AsyncMock,
        side_effect=S3ConfigurationError("bucket not set"),
    ):
        response = client.post(
            "/api/workflows/fasta/upload",
            files={"file": ("single_prediction.fasta", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
            headers={"Authorization": "Bearer testtoken"},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "S3 configuration error" in response.json()["detail"]


def test_upload_fasta_s3_service_error(client):
    """S3ServiceError should map to 502."""
    from app.services.s3 import S3ServiceError

    with patch(
        "app.routes.fasta_upload.upload_file_to_s3",
        new_callable=AsyncMock,
        side_effect=S3ServiceError("upload failed"),
    ):
        response = client.post(
            "/api/workflows/fasta/upload",
            files={"file": ("single_prediction.fasta", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
            headers={"Authorization": "Bearer testtoken"},
        )

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert "S3 upload failed" in response.json()["detail"]


def test_upload_fasta_generic_exception(client):
    """Unexpected exceptions should map to 500."""
    with patch(
        "app.routes.fasta_upload.upload_file_to_s3",
        new_callable=AsyncMock,
        side_effect=RuntimeError("something broke"),
    ):
        response = client.post(
            "/api/workflows/fasta/upload",
            files={"file": ("single_prediction.fasta", BytesIO(b">pro_1\nACDEF\n"), "text/plain")},
            headers={"Authorization": "Bearer testtoken"},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Unexpected error" in response.json()["detail"]
