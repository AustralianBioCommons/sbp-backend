"""Extra coverage for workflow jobs route handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.routes.workflow.jobs import get_job_details


@pytest.mark.asyncio
async def test_get_job_details_success():
    owned_run = SimpleNamespace(workflow=SimpleNamespace(name="BindCraft"), id="rid")
    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={
                "workflow": {
                    "runName": "job-x",
                    "status": "SUCCEEDED",
                    "submit": "2026-02-01T10:00:00Z",
                }
            },
        ),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=0.912,
        ),
    ):
        result = await get_job_details("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())

    assert result.id == "wf-1"
    assert result.workflowType == "BindCraft"
    assert result.score == 0.912


@pytest.mark.asyncio
async def test_get_job_details_not_owned_raises_404():
    with patch("app.routes.workflow.jobs.get_owned_run", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await get_job_details("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())
    assert exc.value.status_code == 404
