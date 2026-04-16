"""PDB file upload routes."""

from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from ..schemas.workflows import PdbDownloadResponse, PdbUploadResponse
from ..services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    upload_file_to_s3,
)
from .dependencies import get_current_user_id

router = APIRouter(tags=["pdb"])

# Maximum file size for PDB uploads (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.post("/upload", response_model=PdbUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_pdb_file(
    file: UploadFile = File(..., description="PDB file to upload"),
) -> PdbUploadResponse:
    """
    Upload a PDB file to S3 private bucket.

    Args:
        file: PDB file from multipart form data

    Returns:
        PdbUploadResponse with upload details

    Raises:
        HTTPException: If validation fails or upload encounters an error
    """
    # Validate file is provided
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided",
        )

    # Validate file extension
    if not file.filename.lower().endswith(".pdb"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must have .pdb extension",
        )

    # Validate file size
    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds 10MB limit",
        )

    # Reset file pointer
    await file.seek(0)

    try:
        # Upload to S3
        upload_result = await upload_file_to_s3(
            file_content=file.file,
            filename=file.filename,
            content_type=file.content_type or "pdb",
            folder="input",
        )

        return PdbUploadResponse(
            message="PDB file uploaded successfully",
            success=upload_result.success,
            fileId=upload_result.file_key,
            fileName=file.filename,
            s3Uri=upload_result.file_url or f"s3://{upload_result.bucket}/{upload_result.file_key}",
            details={
                "bucket": upload_result.bucket,
                "size": len(file_content),
                "content_type": file.content_type,
            },
        )

    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {str(exc)}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 upload failed: {str(exc)}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error during file upload: {str(exc)}",
        ) from exc


@router.get("/download", response_model=PdbDownloadResponse)
async def get_pdb_download_url(
    uri: str = Query(..., description="S3 URI of the PDB file (s3://bucket/key)"),
    _current_user_id: UUID = Depends(get_current_user_id),
) -> PdbDownloadResponse:
    """Return a presigned download URL for a PDB file stored in S3."""
    if not uri.startswith("s3://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URI must be a valid S3 URI (s3://bucket/key)",
        )

    # Strip "s3://" and split into bucket and key
    uri_body = uri[5:]
    slash_idx = uri_body.find("/")
    if slash_idx == -1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid S3 URI: missing object key",
        )

    bucket = uri_body[:slash_idx]
    file_key = uri_body[slash_idx + 1:]

    if not file_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid S3 URI: empty object key",
        )

    # Validate that the bucket matches the configured bucket
    expected_bucket = os.getenv("AWS_S3_BUCKET")
    if expected_bucket and bucket != expected_bucket:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to the specified S3 bucket is not permitted",
        )

    filename = file_key.split("/")[-1] if "/" in file_key else file_key

    try:
        presigned_url = await generate_presigned_url(
            file_key=file_key,
            expiration=3600,
            response_content_disposition=f'attachment; filename="{filename}"',
        )
        return PdbDownloadResponse(url=presigned_url, fileName=filename)

    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {str(exc)}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate download URL: {str(exc)}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error generating download URL: {str(exc)}",
        ) from exc
