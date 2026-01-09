"""S3 file upload service for PDB and other files."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, cast

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
    aws_region = os.getenv("AWS_REGION", "us-east-1")

    if not aws_access_key or not aws_secret_key:
        raise S3ConfigurationError(
            "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
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
        raise S3ConfigurationError("AWS_S3_BUCKET environment variable not set")

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
        error_msg = f"Failed to generate pre-signed URL: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
    except Exception as exc:
        error_msg = f"Unexpected error generating pre-signed URL: {str(exc)}"
        logger.error(error_msg)
        raise S3ServiceError(error_msg) from exc
