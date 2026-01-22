"""Tests for S3 service."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    get_s3_client,
    upload_file_to_s3,
)


@pytest.fixture
def mock_env_vars():
    """Mock environment variables for S3."""
    with patch.dict(
        "os.environ",
        {
            "AWS_ACCESS_KEY_ID": "test-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "AWS_REGION": "us-east-1",
            "AWS_S3_BUCKET": "test-bucket",
        },
    ):
        yield


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    mock_client = MagicMock()
    with patch("app.services.s3.boto3.client", return_value=mock_client):
        yield mock_client


def test_get_s3_client_success(mock_env_vars):
    """Test successful S3 client creation."""
    with patch("app.services.s3.boto3.client") as mock_boto3:
        get_s3_client()
        mock_boto3.assert_called_once_with(
            "s3",
            aws_access_key_id="test-key-id",
            aws_secret_access_key="test-secret-key",
            region_name="us-east-1",
        )


def test_get_s3_client_missing_credentials():
    """Test S3 client creation with missing credentials."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(S3ConfigurationError) as exc_info:
            get_s3_client()
        assert "AWS credentials not configured" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_file_to_s3_success(mock_env_vars, mock_s3_client):
    """Test successful file upload to S3."""
    file_content = BytesIO(b"test content")
    filename = "test.pdb"

    result = await upload_file_to_s3(file_content, filename)

    assert result.success is True
    assert result.bucket == "test-bucket"
    assert filename in result.file_key
    assert "input/" in result.file_key
    assert result.file_url.startswith("s3://")

    mock_s3_client.upload_fileobj.assert_called_once()
    call_args = mock_s3_client.upload_fileobj.call_args
    assert call_args[0][0] == file_content
    assert call_args[0][1] == "test-bucket"
    assert call_args[1]["ExtraArgs"]["ServerSideEncryption"] == "AES256"


@pytest.mark.asyncio
async def test_upload_file_to_s3_missing_bucket():
    """Test upload with missing bucket configuration."""
    with patch.dict(
        "os.environ",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"},
        clear=True,
    ):
        file_content = BytesIO(b"test")
        with pytest.raises(S3ConfigurationError) as exc_info:
            await upload_file_to_s3(file_content, "test.pdb")
        assert "AWS_S3_BUCKET" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_file_to_s3_client_error(mock_env_vars, mock_s3_client):
    """Test upload with S3 client error."""
    mock_s3_client.upload_fileobj.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "Bucket does not exist"}},
        "upload_fileobj",
    )

    file_content = BytesIO(b"test")
    with pytest.raises(S3ServiceError) as exc_info:
        await upload_file_to_s3(file_content, "test.pdb")
    assert "Failed to upload file to S3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_file_with_custom_folder(mock_env_vars, mock_s3_client):
    """Test upload with custom folder."""
    file_content = BytesIO(b"test")
    result = await upload_file_to_s3(file_content, "test.pdb", folder="custom-folder")

    assert "custom-folder/" in result.file_key


@pytest.mark.asyncio
async def test_generate_presigned_url_success(mock_env_vars, mock_s3_client):
    """Test successful pre-signed URL generation."""
    mock_s3_client.generate_presigned_url.return_value = "https://test-url.com"

    url = await generate_presigned_url("input/test.pdb")

    assert url == "https://test-url.com"
    mock_s3_client.generate_presigned_url.assert_called_once()


@pytest.mark.asyncio
async def test_generate_presigned_url_missing_bucket():
    """Test pre-signed URL generation with missing bucket."""
    with patch.dict(
        "os.environ",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"},
        clear=True,
    ):
        with pytest.raises(S3ConfigurationError):
            await generate_presigned_url("test-key")
