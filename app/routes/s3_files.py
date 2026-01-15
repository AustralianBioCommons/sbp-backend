"""Routes for S3 file operations - listing and reading CSV files."""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..services.s3 import (
    S3ConfigurationError,
    S3ServiceError,
    calculate_csv_column_max,
    list_s3_files,
    read_csv_from_s3,
)

router = APIRouter(prefix="/api/s3", tags=["s3-files"])


class S3FileInfo(BaseModel):
    """S3 file information."""

    key: str = Field(..., description="S3 object key")
    size: int = Field(..., description="File size in bytes")
    last_modified: str = Field(..., description="Last modified timestamp")
    bucket: str = Field(..., description="S3 bucket name")


class S3FileListResponse(BaseModel):
    """Response for listing S3 files."""

    files: list[S3FileInfo]
    total: int = Field(..., description="Total number of files")


class CSVDataResponse(BaseModel):
    """Response for CSV data."""

    data: list[dict]
    total_rows: int = Field(..., description="Total number of rows")
    columns: list[str] = Field(..., description="Column names returned")


class MaxScoreResponse(BaseModel):
    """Response for max score calculation."""

    run_id: str = Field(..., description="Run ID")
    max_i_ptm: float = Field(..., description="Maximum i_pTM score")
    total_designs: int = Field(..., description="Number of designs analyzed")
    file_path: str = Field(..., description="S3 file path used")


@router.get("/files", response_model=S3FileListResponse)
async def list_files(
    prefix: str = Query("", description="S3 prefix/folder to filter"),
    extension: str | None = Query(None, description="File extension filter (e.g., .csv)"),
) -> S3FileListResponse:
    """
    List files in S3 bucket with optional filtering.

    Example:
        GET /api/s3/files?prefix=results/ziad-test/ranker/&extension=.csv
    """
    try:
        files = await list_s3_files(prefix=prefix, file_extension=extension)

        return S3FileListResponse(
            files=[S3FileInfo(**file) for file in files],
            total=len(files),
        )

    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {str(exc)}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 service error: {str(exc)}",
        ) from exc


@router.get("/csv/{file_key:path}", response_model=CSVDataResponse)
async def read_csv_file(
    file_key: str,
    columns: list[str] = Query(
        None,
        description="Column names to return (omit to return all columns)",
    ),
) -> CSVDataResponse:
    """
    Read a CSV file from S3 and return selected columns.

    Example:
        GET /api/s3/csv/results/ziad-test/ranker/s1_final_design_stats.csv
        GET /api/s3/csv/results/ziad-test/ranker/s1_final_design_stats.csv?columns=design_id&columns=score
    """
    try:
        data = await read_csv_from_s3(file_key=file_key, columns=columns)

        # Get column names from first row if data exists
        column_names = list(data[0].keys()) if data else []

        return CSVDataResponse(
            data=data,
            total_rows=len(data),
            columns=column_names,
        )

    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {str(exc)}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 service error: {str(exc)}",
        ) from exc


@router.get("/run/{run_id}/max-score", response_model=MaxScoreResponse)
async def get_run_max_score(
    run_id: str,
    folder_prefix: str = Query(
        "results",
        description="S3 folder prefix (default: 'results')",
    ),
    subfolder: str = Query(
        "ranker",
        description="Subfolder within run folder (default: 'ranker')",
    ),
    filename: str = Query(
        "s1_final_design_stats.csv",
        description="CSV filename (default: 's1_final_design_stats.csv')",
    ),
) -> MaxScoreResponse:
    """
    Get maximum i_pTM score for a specific run.

    This endpoint finds the maximum value of the 'Average_i_pTM' column from the
    design statistics CSV file for the specified run.

    Example:
        GET /api/s3/run/ziad-test/max-score
        GET /api/s3/run/ziad-test/max-score?folder_prefix=results&subfolder=ranker

    File path constructed as: {folder_prefix}/{run_id}/{subfolder}/{filename}
    """
    try:
        # Construct file path: results/{run_id}/ranker/s1_final_design_stats.csv
        file_key = f"{folder_prefix}/{run_id}/{subfolder}/{filename}"

        # First check if file exists and get row count
        data = await read_csv_from_s3(file_key=file_key, columns=["Average_i_pTM"])
        total_designs = len(data)

        # Calculate max
        max_score = await calculate_csv_column_max(
            file_key=file_key,
            column_name="Average_i_pTM",
        )

        return MaxScoreResponse(
            run_id=run_id,
            max_i_ptm=max_score,
            total_designs=total_designs,
            file_path=f"s3://{file_key}",
        )

    except S3ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 configuration error: {str(exc)}",
        ) from exc
    except S3ServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found or S3 error: {str(exc)}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid data in CSV: {str(exc)}",
        ) from exc
