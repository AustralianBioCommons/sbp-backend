"""Launch and dataset workflow endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from ...schemas.workflows import (
    DatasetUploadRequest,
    DatasetUploadResponse,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
)
from ...services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
    launch_bindflow_workflow,
)
from ...services.datasets import create_seqera_dataset, upload_dataset_to_seqera

router = APIRouter()


@router.post("/launch", response_model=WorkflowLaunchResponse, status_code=status.HTTP_201_CREATED)
async def launch_workflow(payload: WorkflowLaunchPayload) -> WorkflowLaunchResponse:
    """Launch a workflow on the Seqera Platform."""
    try:
        dataset_id = payload.datasetId
        result: BindflowLaunchResult = await launch_bindflow_workflow(payload.launch, dataset_id)
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return WorkflowLaunchResponse(
        message="Workflow launched successfully",
        runId=result.workflow_id,
        status=result.status,
        submitTime=datetime.now(timezone.utc),
    )


@router.post("/datasets/upload", response_model=DatasetUploadResponse)
async def upload_dataset(payload: DatasetUploadRequest) -> DatasetUploadResponse:
    """Create a Seqera dataset and upload form data as CSV content."""
    try:
        dataset = await create_seqera_dataset(
            name=payload.datasetName, description=payload.datasetDescription
        )
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    await asyncio.sleep(2)

    try:
        upload_result = await upload_dataset_to_seqera(dataset.dataset_id, payload.formData)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except BindflowConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except BindflowExecutorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return DatasetUploadResponse(
        message="Dataset created and uploaded successfully",
        datasetId=upload_result.dataset_id,
        success=upload_result.success,
        details=upload_result.raw_response,
    )
