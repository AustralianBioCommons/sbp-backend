"""Extra coverage for workflow jobs route handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.routes.workflow.jobs import (
    bulk_delete_jobs,
    cancel_workflow,
    delete_job,
    get_job_details,
)
from app.schemas.workflows import BulkDeleteJobsRequest
from app.services.seqera import SeqeraAPIError


@pytest.mark.asyncio
async def test_cancel_workflow_not_owned_raises_404():
    with patch("app.routes.workflow.jobs.get_owned_run", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await cancel_workflow("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_workflow_api_error_maps_502():
    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=object()),
        patch("app.routes.workflow.jobs.cancel_seqera_workflow", new_callable=AsyncMock, side_effect=SeqeraAPIError("down")),
    ):
        with pytest.raises(HTTPException) as exc:
            await cancel_workflow("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_job_details_success():
    owned_run = SimpleNamespace(workflow=SimpleNamespace(name="BindCraft"), id="rid")
    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
        patch("app.routes.workflow.jobs.describe_workflow", new_callable=AsyncMock, return_value={"workflow": {"runName": "job-x", "status": "SUCCEEDED", "submit": "2026-02-01T10:00:00Z"}}),
        patch("app.routes.workflow.jobs.ensure_completed_run_score", new_callable=AsyncMock, return_value=0.912),
    ):
        result = await get_job_details("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())

    assert result.id == "wf-1"
    assert result.workflowType == "BindCraft"
    assert result.score == 0.912


@pytest.mark.asyncio
async def test_delete_job_success_cancels_running_and_deletes_local_rows():
    db = Mock()
    owned_run = SimpleNamespace(id="rid", workflow=None)
    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
        patch("app.routes.workflow.jobs.describe_workflow", new_callable=AsyncMock, return_value={"workflow": {"status": "RUNNING"}}),
        patch("app.routes.workflow.jobs.cancel_seqera_workflow", new_callable=AsyncMock, return_value=None),
        patch("app.routes.workflow.jobs.delete_seqera_workflow", new_callable=AsyncMock, return_value=None),
    ):
        resp = await delete_job("wf-1", UUID("11111111-1111-1111-1111-111111111111"), db)

    assert resp.deleted is True
    assert resp.cancelledBeforeDelete is True
    assert db.execute.call_count == 3
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_bulk_delete_jobs_mixed_results():
    db = Mock()

    def _owned(_db, _uid, run_id):
        return None if run_id == "missing" else SimpleNamespace(id=f"id-{run_id}")

    with (
        patch("app.routes.workflow.jobs.get_owned_run", side_effect=_owned),
        patch("app.routes.workflow.jobs.describe_workflow", new_callable=AsyncMock, return_value={"workflow": {"status": "FAILED"}}),
        patch("app.routes.workflow.jobs.delete_seqera_workflow", new_callable=AsyncMock, return_value=None),
    ):
        out = await bulk_delete_jobs(
            BulkDeleteJobsRequest(runIds=["ok", "missing"]),
            UUID("11111111-1111-1111-1111-111111111111"),
            db,
        )

    assert out.deleted == ["ok"]
    assert out.failed["missing"] == "Job not found"
