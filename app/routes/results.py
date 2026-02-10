"""Results-specific HTTP routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..schemas.workflows import JobSettingParamsResponse
from ..services.job_utils import get_owned_run
from ..services.results_utils import resolve_submitted_form_data
from .dependencies import get_current_user_id, get_db

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
