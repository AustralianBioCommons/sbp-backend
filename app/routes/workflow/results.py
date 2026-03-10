"""Results-specific HTTP routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...schemas.workflows import JobSettingParamsResponse, ResultLogsResponse
from ...services.job_utils import get_owned_run
from ...services.results_utils import format_log_entries, resolve_submitted_form_data
from ...services.seqera_client import get_workflow_logs_raw
from ...services.seqera_errors import SeqeraAPIError, SeqeraConfigurationError
from ..dependencies import get_current_user_id, get_db

router = APIRouter(tags=["results"])


@router.get("/{run_id}/settingParams", response_model=JobSettingParamsResponse)
async def get_result_setting_params(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> JobSettingParamsResponse:
    """Return the submitted form settings for a workflow result view."""
    owned_run = get_owned_run(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return JobSettingParamsResponse(
        runId=run_id,
        settingParams=resolve_submitted_form_data(owned_run),
    )


@router.get("/{run_id}/logs", response_model=ResultLogsResponse)
async def get_result_logs(
    run_id: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ResultLogsResponse:
    """Return Seqera workflow logs for a workflow result view."""
    owned_run = get_owned_run(db, current_user_id, run_id)
    if not owned_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    try:
        payload = await get_workflow_logs_raw(run_id)
    except SeqeraConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except SeqeraAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Seqera may return either the log object directly or wrapped under a top-level "log" key.
    log_payload: dict[str, Any]
    nested_log = payload.get("log")
    if isinstance(nested_log, dict):
        log_payload = nested_log
    else:
        log_payload = payload

    entries = log_payload.get("entries")
    normalized_entries = [str(item) for item in entries] if isinstance(entries, list) else []

    return ResultLogsResponse(
        runId=run_id,
        truncated=bool(log_payload.get("truncated", False)),
        pending=bool(log_payload.get("pending", False)),
        message="" if log_payload.get("message") is None else str(log_payload.get("message", "")),
        rewindToken=""
        if log_payload.get("rewindToken") is None
        else str(log_payload.get("rewindToken", "")),
        forwardToken=""
        if log_payload.get("forwardToken") is None
        else str(log_payload.get("forwardToken", "")),
        downloads=log_payload.get("downloads", [])
        if isinstance(log_payload.get("downloads"), list)
        else [],
        entries=normalized_entries,
        formattedEntries=format_log_entries(normalized_entries),
    )
