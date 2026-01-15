"""S3 file upload service for PDB and other files."""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class S3ServiceError(Exception):
    """Raised when S3 operations fail."""


class S3ConfigurationError(Exception):
    """Raised when S3 configuration is invalid or missing."""


@dataclass
class S3UploadResult:
    """Result of an S3 upload operation."""

    success: bool
    file_key: str
    bucket: str
    file_url: str | None = None
    error: str | None = None


def get_s3_client():
    """Get configured S3 client."""
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION", "ap-southeast-2")

    if not aws_access_key or not aws_secret_key:
        raise S3ConfigurationError(
            "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the .env file."
        )

    return boto3.client(
        "s3",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
    )


async def upload_file_to_s3(
    file_content: BinaryIO,
    filename: str,
    content_type: str = "application/octet-stream",
    folder: str = "input",
) -> S3UploadResult:
    """
    Upload a file to S3 private bucket.

    Args:
        file_content: Binary file content
        filename: Original filename
        content_type: MIME type of the file
        folder: S3 folder/prefix to store the file

    Returns:
        S3UploadResult with upload details

    Raises:
        S3ConfigurationError: If S3 is not properly configured
        S3ServiceError: If upload fails
    """
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if not bucket_name:
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set")

    try:
        s3_client = get_s3_client()

        # Generate unique file key with timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file_key = f"{folder}/{timestamp}_{filename}"

        # Upload file to S3
        s3_client.upload_fileobj(
            file_content,
            bucket_name,
            file_key,
            ExtraArgs={
                "ContentType": content_type,
                "ServerSideEncryption": "AES256",  # Encrypt at rest
            },
        )

        logger.info("Successfully uploaded %s to s3://%s/%s", filename, bucket_name, file_key)

        # Generate file URL (for private bucket, this would need pre-signed URL for access)
        file_url = f"s3://{bucket_name}/{file_key}"

        return S3UploadResult(
            success=True,
            file_key=file_key,
            bucket=bucket_name,
            file_url=file_url,
        )

    except (BotoCoreError, ClientError) as exc:
        error_msg = f"Failed to upload file to S3: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except Exception as exc:
        error_msg = f"Unexpected error during S3 upload: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc


async def generate_presigned_url(
    file_key: str,
    expiration: int = 3600,
) -> str:
    """
    Generate a pre-signed URL for private S3 object access.

    Args:
        file_key: S3 object key
        expiration: URL expiration time in seconds (default 1 hour)

    Returns:
        Pre-signed URL string

    Raises:
        S3ConfigurationError: If S3 is not properly configured
        S3ServiceError: If URL generation fails
    """
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if not bucket_name:
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set in env file!")

    try:
        s3_client = get_s3_client()
        url = cast(
            str,
            s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name, "Key": file_key},
                ExpiresIn=expiration,
            ),
        )
        return url

    except (BotoCoreError, ClientError) as exc:
        error_msg = f"Failed to generate pre-signed URI: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except Exception as exc:
        error_msg = f"Unexpected error generating pre-signed URL: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc


async def list_s3_files(
    prefix: str = "",
    file_extension: str | None = None,
) -> list[dict[str, Any]]:
    """
    List files in S3 bucket with optional filtering.

    Args:
        prefix: S3 prefix/folder to filter (e.g., "results/ziad-test/ranker/")
        file_extension: Filter by file extension (e.g., ".csv", ".json")

    Returns:
        List of file objects with key, size, and last_modified

    Raises:
        S3ConfigurationError: If S3 is not properly configured
        S3ServiceError: If listing fails
    """
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if not bucket_name:
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set")

    try:
        s3_client = get_s3_client()

        files = []
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

        for page in pages:
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                file_key = obj["Key"]

                # Filter by extension if specified
                if file_extension and not file_key.endswith(file_extension):
                    continue

                files.append(
                    {
                        "key": file_key,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                        "bucket": bucket_name,
                    }
                )

        logger.info("Listed %d files from s3://%s/%s", len(files), bucket_name, prefix)
        return files

    except (BotoCoreError, ClientError) as exc:
        error_msg = f"Failed to list S3 files: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except Exception as exc:
        error_msg = f"Unexpected error listing S3 files: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc


async def read_csv_from_s3(
    file_key: str,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Read a CSV file from S3 and return selected columns.

    Args:
        file_key: S3 object key (e.g., "results/ziad-test/ranker/s1_final_design_stats.csv")
        columns: List of column names to return. If None, returns all columns.

    Returns:
        List of dictionaries, each representing a row with selected columns

    Raises:
        S3ConfigurationError: If S3 is not properly configured
        S3ServiceError: If read fails
    """
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if not bucket_name:
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set")

    try:
        s3_client = get_s3_client()

        # Download file content
        response = s3_client.get_object(Bucket=bucket_name, Key=file_key)
        content = response["Body"].read().decode("utf-8")

        # Parse CSV
        csv_reader = csv.DictReader(io.StringIO(content))
        rows = []

        for row in csv_reader:
            if columns:
                # Filter to only selected columns
                filtered_row = {col: row.get(col) for col in columns if col in row}
                rows.append(filtered_row)
            else:
                # Return all columns
                rows.append(dict(row))

        logger.info(
            "Read %d rows from s3://%s/%s (columns: %s)",
            len(rows),
            bucket_name,
            file_key,
            columns or "all",
        )
        return rows

    except (BotoCoreError, ClientError) as exc:
        error_msg = f"Failed to read CSV from S3: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except csv.Error as exc:
        error_msg = f"Failed to parse CSV file: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except Exception as exc:
        error_msg = f"Unexpected error reading CSV from S3: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc


async def calculate_csv_column_max(
    file_key: str,
    column_name: str,
) -> float:
    """
    Calculate the maximum value of a numeric column in a CSV file.

    Args:
        file_key: S3 object key (e.g., "results/run-123/ranker/s1_final_design_stats.csv")
        column_name: Name of the column to find max (e.g., "Average_i_pTM")

    Returns:
        Maximum value of the specified column

    Raises:
        S3ConfigurationError: If S3 is not properly configured
        S3ServiceError: If read fails or column not found
        ValueError: If column contains non-numeric values
    """
    bucket_name = os.getenv("AWS_S3_BUCKET")
    if not bucket_name:
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set")

    try:
        s3_client = get_s3_client()

        # Download file content
        response = s3_client.get_object(Bucket=bucket_name, Key=file_key)
        content = response["Body"].read().decode("utf-8")

        # Parse CSV
        csv_reader = csv.DictReader(io.StringIO(content))
        values = []

        for row in csv_reader:
            if column_name not in row:
                raise S3ServiceError(f"Column '{column_name}' not found in CSV file")

            value = row[column_name]
            if value and value.strip():  # Skip empty values
                try:
                    values.append(float(value))
                except ValueError as exc:
                    raise ValueError(
                        f"Column '{column_name}' contains non-numeric value: {value}"
                    ) from exc

        if not values:
            raise S3ServiceError(f"No valid numeric values found in column '{column_name}'")

        max_value = max(values)

        logger.info(
            "Calculated max of column '%s' from s3://%s/%s: %.4f (n=%d)",
            column_name,
            bucket_name,
            file_key,
            max_value,
            len(values),
        )
        return max_value

    except (BotoCoreError, ClientError) as exc:
        error_msg = f"Failed to read CSV from S3: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except csv.Error as exc:
        error_msg = f"Failed to parse CSV file: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
