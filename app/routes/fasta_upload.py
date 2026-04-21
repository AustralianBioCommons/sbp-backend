"""FASTA file upload routes."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from ..schemas.workflows import FastaUploadResponse
from ..services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    generate_presigned_url,
    upload_file_to_s3,
)

router = APIRouter(tags=["fasta"])

MAX_FILE_SIZE = 10 * 1024 * 1024


@router.post("/upload", response_model=FastaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_fasta_file(
    file: UploadFile = File(..., description="FASTA file to upload"),
) -> FastaUploadResponse:
    """Upload a FASTA file to S3 and return a pre-signed URL."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided",
        )

    if not file.filename.lower().endswith((".fa", ".fasta", ".faa", ".fna")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must have .fa, .fasta, .faa, or .fna extension",
        )

    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds 10MB limit",
        )

    if not file_content.strip().startswith(b">"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="FASTA content must start with a header line beginning with >",
        )

    await file.seek(0)

    try:
        upload_result = await upload_file_to_s3(
            file_content=file.file,
            filename=file.filename,
            content_type=file.content_type or "text/plain",
            folder="input",
        )
        presigned_url = await generate_presigned_url(
            upload_result.file_key,
            response_content_type="text/plain",
            response_content_disposition="inline",
        )

        return FastaUploadResponse(
            message="FASTA file uploaded successfully",
            success=upload_result.success,
            fileId=upload_result.file_key,
            fileName=file.filename,
            s3Uri=upload_result.file_url
            or f"s3://{upload_result.bucket}/{upload_result.file_key}",
            presignedUrl=presigned_url,
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
