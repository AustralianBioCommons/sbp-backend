"""Tests for S3 service."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    calculate_csv_column_max,
    generate_presigned_url,
    get_s3_client,
    list_s3_files,
    read_csv_from_s3,
    read_s3_bytes,
    read_s3_file,
    upload_file_to_s3,
)


@pytest.fixture
def mock_s3_settings(mock_settings):
    """Mock settings with S3 fields configured for service tests."""
    mock_settings.aws.access_key_id = "test-key-id"
    mock_settings.aws.secret_access_key = "test-secret-key"
    mock_settings.aws.region = "us-east-1"
    mock_settings.aws.s3_bucket = "test-bucket"
    return mock_settings


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    mock_client = MagicMock()
    with patch("app.services.s3.boto3.client", return_value=mock_client):
        yield mock_client


def test_get_s3_client_success(mock_s3_settings):
    """Test successful S3 client creation."""
    with patch("app.services.s3.boto3.client") as mock_boto3:
        get_s3_client(settings=mock_s3_settings)
        mock_boto3.assert_called_once_with(
            "s3",
            aws_access_key_id="test-key-id",
            aws_secret_access_key="test-secret-key",
            region_name="us-east-1",
        )


def test_get_s3_client_missing_credentials(mock_s3_settings):
    """Test S3 client creation with missing credentials."""
    mock_s3_settings.aws.access_key_id = ""
    mock_s3_settings.aws.secret_access_key = ""
    mock_s3_settings.aws.region = "ap-southeast-2"
    with patch("app.services.s3.boto3.client") as mock_boto3:
        get_s3_client(settings=mock_s3_settings)
        mock_boto3.assert_called_once_with("s3", region_name="ap-southeast-2")


@pytest.mark.asyncio
async def test_upload_file_to_s3_success(mock_s3_settings, mock_s3_client):
    """Test successful file upload to S3."""
    file_content = BytesIO(b"test content")
    filename = "test.pdb"

    result = await upload_file_to_s3(file_content, filename, settings=mock_s3_settings)

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
async def test_upload_file_to_s3_missing_bucket(mock_s3_settings):
    """Test upload with missing bucket configuration."""
    mock_s3_settings.aws.s3_bucket = ""
    file_content = BytesIO(b"test")
    with pytest.raises(S3ConfigurationError) as exc_info:
        await upload_file_to_s3(file_content, "test.pdb", settings=mock_s3_settings)
    assert "AWS_S3_BUCKET" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_file_to_s3_client_error(mock_s3_settings, mock_s3_client):
    """Test upload with S3 client error."""
    mock_s3_client.upload_fileobj.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "Bucket does not exist"}},
        "upload_fileobj",
    )

    file_content = BytesIO(b"test")
    with pytest.raises(S3ServiceError) as exc_info:
        await upload_file_to_s3(file_content, "test.pdb", settings=mock_s3_settings)
    assert "Failed to upload file to S3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_file_with_custom_folder(mock_s3_settings, mock_s3_client):
    """Test upload with custom folder."""
    file_content = BytesIO(b"test")
    result = await upload_file_to_s3(
        file_content, "test.pdb", folder="custom-folder", settings=mock_s3_settings
    )

    assert "custom-folder/" in result.file_key


@pytest.mark.asyncio
async def test_generate_presigned_url_success(mock_s3_settings, mock_s3_client):
    """Test successful pre-signed URL generation."""
    mock_s3_client.generate_presigned_url.return_value = "https://test-url.com"

    url = await generate_presigned_url("input/test.pdb", settings=mock_s3_settings)

    assert url == "https://test-url.com"
    mock_s3_client.generate_presigned_url.assert_called_once()


@pytest.mark.asyncio
async def test_generate_presigned_url_missing_bucket(mock_s3_settings):
    """Test pre-signed URL generation with missing bucket."""
    mock_s3_settings.aws.s3_bucket = ""
    with pytest.raises(S3ConfigurationError):
        await generate_presigned_url("test-key", settings=mock_s3_settings)


# ============================================================================
# Tests for new S3 CSV functions
# ============================================================================


@pytest.mark.asyncio
async def test_list_s3_files_success(mock_s3_settings, mock_s3_client):
    """Test successful S3 file listing."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {
                    "Key": "results/test/file1.csv",
                    "Size": 1024,
                    "LastModified": datetime(2026, 1, 15, tzinfo=UTC),
                },
                {
                    "Key": "results/test/file2.csv",
                    "Size": 2048,
                    "LastModified": datetime(2026, 1, 15, tzinfo=UTC),
                },
            ]
        }
    ]
    mock_s3_client.get_paginator.return_value = mock_paginator

    files = await list_s3_files(prefix="results/test/", settings=mock_s3_settings)

    assert len(files) == 2
    assert files[0]["key"] == "results/test/file1.csv"
    assert files[0]["size"] == 1024
    assert files[0]["bucket"] == "test-bucket"


@pytest.mark.asyncio
async def test_list_s3_files_with_extension_filter(mock_s3_settings, mock_s3_client):
    """Test file listing with extension filter."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": "test/file1.csv", "Size": 1024, "LastModified": datetime.now(UTC)},
                {"Key": "test/file2.txt", "Size": 2048, "LastModified": datetime.now(UTC)},
                {"Key": "test/file3.csv", "Size": 512, "LastModified": datetime.now(UTC)},
            ]
        }
    ]
    mock_s3_client.get_paginator.return_value = mock_paginator

    files = await list_s3_files(prefix="test/", file_extension=".csv", settings=mock_s3_settings)

    assert len(files) == 2
    assert all(f["key"].endswith(".csv") for f in files)


@pytest.mark.asyncio
async def test_list_s3_files_empty_result(mock_s3_settings, mock_s3_client):
    """Test file listing with no results."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{}]  # No Contents key
    mock_s3_client.get_paginator.return_value = mock_paginator

    files = await list_s3_files(prefix="nonexistent/", settings=mock_s3_settings)

    assert len(files) == 0


@pytest.mark.asyncio
async def test_list_s3_files_client_error(mock_s3_settings, mock_s3_client):
    """Test file listing handles ClientError."""
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
        "list_objects_v2",
    )
    mock_s3_client.get_paginator.return_value = mock_paginator

    with pytest.raises(S3ServiceError, match="Failed to list S3 files"):
        await list_s3_files(prefix="test/", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_read_s3_file_success(mock_s3_settings, mock_s3_client):
    """Test reading a text object from S3."""
    content = "rank\tscore\n0\t0.91\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    result = await read_s3_file("results/test/file.tsv", settings=mock_s3_settings)

    assert result == content
    mock_s3_client.get_object.assert_called_once_with(
        Bucket="test-bucket",
        Key="results/test/file.tsv",
    )


@pytest.mark.asyncio
async def test_read_s3_file_missing_bucket(mock_s3_settings):
    """Test reading a text object without bucket configuration."""
    mock_s3_settings.aws.s3_bucket = ""
    with pytest.raises(S3ConfigurationError, match="AWS_S3_BUCKET"):
        await read_s3_file("results/test/file.tsv", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_read_s3_file_client_error(mock_s3_settings, mock_s3_client):
    """Test reading a text object wraps S3 client errors."""
    mock_s3_client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "get_object",
    )

    with pytest.raises(S3ServiceError, match="Failed to read CSV from S3"):
        await read_s3_file("results/test/missing.tsv", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_read_s3_bytes_success(mock_s3_settings, mock_s3_client):
    """Test reading a bytes object from S3."""
    content = b"rank\tscore\n0\t0.91\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = content
    mock_s3_client.get_object.return_value = mock_response

    result = await read_s3_bytes("results/test/file.tsv", settings=mock_s3_settings)

    assert result == content
    mock_s3_client.get_object.assert_called_once_with(
        Bucket="test-bucket",
        Key="results/test/file.tsv",
    )


@pytest.mark.asyncio
async def test_read_s3_bytes_missing_bucket(mock_s3_settings):
    """Test reading a text object without bucket configuration."""
    mock_s3_settings.aws.s3_bucket = ""
    with pytest.raises(S3ConfigurationError, match="AWS_S3_BUCKET"):
        await read_s3_bytes("results/test/file.tsv", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_read_s3_bytes_client_error(mock_s3_settings, mock_s3_client):
    """Test reading a text object wraps S3 client errors."""
    mock_s3_client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "get_object",
    )

    with pytest.raises(S3ServiceError, match="Failed to read file from S3"):
        await read_s3_bytes("results/test/missing.tsv", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_read_csv_from_s3_all_columns(mock_s3_settings, mock_s3_client):
    """Test reading CSV with all columns."""
    csv_content = "Design,Average_i_pTM,Rank\ndesign1,0.84,1\ndesign2,0.78,2\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    data = await read_csv_from_s3("results/test/file.csv", settings=mock_s3_settings)

    assert len(data) == 2
    assert data[0]["Design"] == "design1"
    assert data[0]["Average_i_pTM"] == "0.84"
    assert data[1]["Rank"] == "2"


@pytest.mark.asyncio
async def test_read_csv_from_s3_selected_columns(mock_s3_settings, mock_s3_client):
    """Test reading CSV with selected columns."""
    csv_content = "Design,Average_i_pTM,Rank,Extra\ndesign1,0.84,1,value1\ndesign2,0.78,2,value2\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    data = await read_csv_from_s3(
        "results/test/file.csv", columns=["Design", "Average_i_pTM"], settings=mock_s3_settings
    )

    assert len(data) == 2
    assert list(data[0].keys()) == ["Design", "Average_i_pTM"]
    assert "Rank" not in data[0]
    assert "Extra" not in data[0]


@pytest.mark.asyncio
async def test_read_csv_from_s3_empty_file(mock_s3_settings, mock_s3_client):
    """Test reading empty CSV file."""
    csv_content = "Design,Average_i_pTM,Rank\n"  # Header only
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    data = await read_csv_from_s3("results/test/file.csv", settings=mock_s3_settings)

    assert len(data) == 0


@pytest.mark.asyncio
async def test_read_csv_from_s3_file_not_found(mock_s3_settings, mock_s3_client):
    """Test reading non-existent CSV file."""
    mock_s3_client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "get_object",
    )

    with pytest.raises(S3ServiceError, match="Failed to read CSV from S3"):
        await read_csv_from_s3("nonexistent/file.csv", settings=mock_s3_settings)


@pytest.mark.asyncio
async def test_calculate_csv_column_max_success(mock_s3_settings, mock_s3_client):
    """Test successful max calculation."""
    csv_content = "Design,Average_i_pTM\ndesign1,0.84\ndesign2,0.78\ndesign3,0.92\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    max_value = await calculate_csv_column_max(
        "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
    )

    assert max_value == 0.92


@pytest.mark.asyncio
async def test_calculate_csv_column_max_with_empty_values(mock_s3_settings, mock_s3_client):
    """Test max calculation skips empty values."""
    csv_content = "Design,Average_i_pTM\ndesign1,0.84\ndesign2,\ndesign3,0.92\ndesign4,  \n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    max_value = await calculate_csv_column_max(
        "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
    )

    assert max_value == 0.92


@pytest.mark.asyncio
async def test_calculate_csv_column_max_column_not_found(mock_s3_settings, mock_s3_client):
    """Test max calculation fails when column doesn't exist."""
    csv_content = "Design,Score\ndesign1,0.84\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    with pytest.raises(S3ServiceError, match="Column 'Average_i_pTM' not found"):
        await calculate_csv_column_max(
            "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
        )


@pytest.mark.asyncio
async def test_calculate_csv_column_max_non_numeric_values(mock_s3_settings, mock_s3_client):
    """Test max calculation fails with non-numeric values."""
    csv_content = "Design,Average_i_pTM\ndesign1,invalid\ndesign2,0.78\n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    with pytest.raises(ValueError, match="non-numeric value"):
        await calculate_csv_column_max(
            "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
        )


@pytest.mark.asyncio
async def test_calculate_csv_column_max_no_valid_values(mock_s3_settings, mock_s3_client):
    """Test max calculation fails when no valid values exist."""
    csv_content = "Design,Average_i_pTM\ndesign1,\ndesign2,  \n"
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value = csv_content.encode("utf-8")
    mock_s3_client.get_object.return_value = mock_response

    with pytest.raises(S3ServiceError, match="No valid numeric values found"):
        await calculate_csv_column_max(
            "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
        )


@pytest.mark.asyncio
async def test_calculate_csv_column_max_missing_bucket(mock_s3_settings):
    """Test max calculation fails without bucket configuration."""
    mock_s3_settings.aws.s3_bucket = ""
    with pytest.raises(S3ConfigurationError, match="AWS_S3_BUCKET"):
        await calculate_csv_column_max(
            "results/test/file.csv", "Average_i_pTM", settings=mock_s3_settings
        )
