"""Extra coverage for workflow jobs route handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.core import (
    AppUser,
    RunInput,
    RunMetric,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)
from app.routes.workflow.jobs import (
    bulk_delete_jobs,
    cancel_workflow,
    delete_job,
    get_job_details,
)
from app.schemas.workflows import BulkDeleteJobsRequest
from app.services.seqera_errors import SeqeraAPIError


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
        patch(
            "app.routes.workflow.jobs.cancel_workflow_raw",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("down"),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await cancel_workflow("wf-1", UUID("11111111-1111-1111-1111-111111111111"), Mock())
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_job_details_success(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user",
        name="User",
        email="user@example.com",
    )
    workflow = Workflow(id=uuid4(), name="BindCraft", description="Binding workflow")
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        workflow_id=workflow.id,
        seqera_run_id="wf-1",
        work_dir="workdir-1",
    )
    test_db.add_all([user, workflow, run])
    test_db.commit()

    with (
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
        result = await get_job_details("wf-1", user.id, test_db)

    assert result.id == "wf-1"
    assert result.workflowType == "BindCraft"
    assert result.score == 0.912


@pytest.mark.asyncio
async def test_delete_job_success_cancels_running_and_deletes_local_rows(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user",
        name="User",
        email="user@example.com",
    )
    test_db.add(user)
    test_db.commit()

    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-1",
        work_dir="workdir-1",
    )
    s3_in = S3Object(object_key="in-1", uri="s3://bucket/in-1")
    s3_out = S3Object(object_key="out-1", uri="s3://bucket/out-1")
    test_db.add_all([run, s3_in, s3_out])
    test_db.commit()

    test_db.add_all(
        [
            RunInput(run_id=run.id, s3_object_id=s3_in.object_key),
            RunOutput(run_id=run.id, s3_object_id=s3_out.object_key),
            RunMetric(run_id=run.id, max_score=1.23),
        ]
    )
    test_db.commit()

    with (
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "RUNNING"}},
        ),
        patch(
            "app.routes.workflow.jobs.cancel_workflow_raw",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_cancel,
        patch(
            "app.routes.workflow.jobs.delete_workflow_raw",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_delete,
    ):
        resp = await delete_job("wf-1", user.id, test_db)

    assert resp.deleted is True
    assert resp.cancelledBeforeDelete is True
    mock_cancel.assert_awaited_once_with("wf-1")
    mock_delete.assert_awaited_once_with("wf-1")

    assert test_db.get(WorkflowRun, run.id) is None
    assert test_db.execute(select(RunInput).where(RunInput.run_id == run.id)).first() is None
    assert test_db.execute(select(RunOutput).where(RunOutput.run_id == run.id)).first() is None
    assert test_db.execute(select(RunMetric).where(RunMetric.run_id == run.id)).first() is None


@pytest.mark.asyncio
async def test_bulk_delete_jobs_mixed_results():
    db = Mock()

    def _owned(_db, _uid, run_id):
        return None if run_id == "missing" else SimpleNamespace(id=f"id-{run_id}")

    with (
        patch("app.routes.workflow.jobs.get_owned_run", side_effect=_owned),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "FAILED"}},
        ),
        patch(
            "app.routes.workflow.jobs.delete_workflows_raw",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_delete,
    ):
        out = await bulk_delete_jobs(
            BulkDeleteJobsRequest(runIds=["ok", "missing"]),
            UUID("11111111-1111-1111-1111-111111111111"),
            db,
        )

    assert out.deleted == ["ok"]
    assert out.failed["missing"] == "Job not found"
    mock_delete.assert_called_once_with(["ok"])
