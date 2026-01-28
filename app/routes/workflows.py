"""Workflow-related HTTP routes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status

from ..schemas.workflows import (
    CancelWorkflowResponse,
    DatasetUploadRequest,
    DatasetUploadResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
)
from ..services.datasets import (
    create_seqera_dataset,
    upload_dataset_to_seqera,
)
from ..services.seqera import (
    SeqeraConfigurationError,
    SeqeraLaunchResult,
    SeqeraServiceError,
    launch_seqera_workflow,
)

router = APIRouter(tags=["workflows"])
logger = logging.getLogger(__name__)


@router.post("/launch", response_model=WorkflowLaunchResponse, status_code=status.HTTP_201_CREATED)
async def launch_workflow(payload: WorkflowLaunchPayload) -> WorkflowLaunchResponse:
    """Launch a workflow on the Seqera Platform."""
    try:
        dataset_id = payload.datasetId

        # If formData and pdbFileKey are provided, create dataset with PDB S3 URI
        if payload.formData and payload.pdbFileKey:
            try:
                # Get S3 bucket from environment
                import os
                bucket = os.getenv("AWS_S3_BUCKET", "unknown-bucket")
                s3_uri = f"s3://{bucket}/{payload.pdbFileKey}"
                
                # Replace starting_pdb with full S3 URI (this is what the workflow needs)
                payload.formData["starting_pdb"] = s3_uri
                
                # Also add these for reference
                payload.formData["pdb_file_s3_uri"] = s3_uri
                payload.formData["pdb_file_key"] = payload.pdbFileKey
                
                logger.info(f"Updated formData with S3 URI: {s3_uri}")
                
                # Create dataset with formData including PDB info
                dataset = await create_seqera_dataset(
                    name=f"dataset-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
                    description="Dataset with PDB file reference"
                )
                await asyncio.sleep(2)
                
                upload_result = await upload_dataset_to_seqera(dataset.dataset_id, payload.formData)
                dataset_id = upload_result.dataset_id
            except Exception as e:
                logger.error(f"Failed to create dataset with PDB file: {e}", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create dataset with PDB file: {str(e)}"
                ) from e

        # Use the dataset created from /datasets/upload endpoint or created above
        result: SeqeraLaunchResult = await launch_seqera_workflow(payload.launch, dataset_id)
    except HTTPException:
        raise
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Unexpected error in workflow launch: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(exc)}"
        ) from exc

    return WorkflowLaunchResponse(
        message="Workflow launched successfully",
        runId=result.workflow_id,
        status=result.status,
        submitTime=datetime.now(timezone.utc),
    )


@router.post("/{run_id}/cancel", response_model=CancelWorkflowResponse)
async def cancel_workflow(run_id: str) -> CancelWorkflowResponse:
    """Cancel a workflow run (placeholder implementation)."""
    return CancelWorkflowResponse(
        message="Workflow cancelled successfully",
        runId=run_id,
        status="cancelled",
    )


@router.get("/runs", response_model=ListRunsResponse)
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    workspace: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ListRunsResponse:
    """List workflow runs (placeholder until Seqera list API integration)."""
    _ = (status_filter, workspace)  # Reserved for future Seqera integration
    return ListRunsResponse(runs=[], total=0, limit=limit, offset=offset)


@router.get("/{run_id}/logs", response_model=LaunchLogs)
async def get_logs() -> LaunchLogs:
    """Retrieve workflow logs (placeholder)."""
    return LaunchLogs(
        truncated=False,
        entries=[],
        rewindToken="",
        forwardToken="",
        pending=False,
        message="Logs endpoint - implementation pending",
        downloads=[],
    )


@router.get("/{run_id}/details", response_model=LaunchDetails)
async def get_details(run_id: str) -> LaunchDetails:
    """Return workflow details (placeholder)."""
    iso_now = datetime.now(timezone.utc).isoformat()
    return LaunchDetails(
        requiresAttention=False,
        status="UNKNOWN",
        ownerId=0,
        repository="",
        id=run_id,
        submit="",
        start="",
        complete="",
        dateCreated=iso_now,
        lastUpdated=iso_now,
        runName="",
        sessionId="",
        profile="",
        workDir="",
        commitId="",
        userName="",
        scriptId="",
        revision="",
        commandLine="",
        projectName="",
        scriptName="",
        launchId="",
        configFiles=[],
        params={},
    )


@router.post("/datasets/upload", response_model=DatasetUploadResponse)
async def upload_dataset(payload: DatasetUploadRequest) -> DatasetUploadResponse:
    """Create a Seqera dataset and upload form data as CSV content."""

    # If pdbFileKey is provided, replace starting_pdb with full S3 URI
    if payload.pdbFileKey:
        import os
        bucket = os.getenv("AWS_S3_BUCKET", "unknown-bucket")
        s3_uri = f"s3://{bucket}/{payload.pdbFileKey}"
        
        # Replace starting_pdb with full S3 URI (this is what the workflow needs)
        payload.formData["starting_pdb"] = s3_uri
        
    else:
        logger.warning("No pdbFileKey provided in request")
    
    try:
        dataset = await create_seqera_dataset(
            name=payload.datasetName, description=payload.datasetDescription
        )
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Allow Seqera time to finish dataset initialization before uploading
    await asyncio.sleep(2)

    try:
        upload_result = await upload_dataset_to_seqera(dataset.dataset_id, payload.formData)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return DatasetUploadResponse(
        message="Dataset created and uploaded successfully",
        datasetId=upload_result.dataset_id,
        success=upload_result.success,
        details=upload_result.raw_response,
    )
