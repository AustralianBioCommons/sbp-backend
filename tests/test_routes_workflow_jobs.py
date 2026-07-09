"""Tests for job listing and details endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.models.core import Workflow, WorkflowRun
from app.main import create_app
from app.routes.workflow.jobs import get_job_details, list_jobs
from tests.datagen import (
    AppUserFactory,
    QueuedJobFactory,
    UserJobListRowFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


@pytest.fixture
def mock_db(mocker):
    """Mock database session."""
    return mocker.Mock()


@pytest.fixture
def mock_user_id():
    """Create a mock user ID."""
    return uuid4()


@pytest.mark.asyncio
async def test_list_jobs_success(mock_db, mock_user_id):
    """Test successful job listing."""
    run_id = "run-123"
    seqera_run_id = "wf-123"

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[
                UserJobListRowFactory.build(
                    run_id=run_id,
                    seqera_run_id=seqera_run_id,
                    workflow_type="BindCraft",
                    tool="BindCraft",
                    score=0.95,
                )
            ],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={
                "workflow": {
                    "id": seqera_run_id,
                    "runName": "Test Job",
                    "status": "SUCCEEDED",
                    "submit": "2026-02-01T10:00:00Z",
                }
            },
        ),
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert response.total == 1
    assert len(response.jobs) == 1
    assert response.jobs[0].id == run_id
    assert response.jobs[0].jobName == "Test Job"
    assert response.jobs[0].status == "Completed"
    assert response.jobs[0].workflow == "BindCraft"


@pytest.mark.asyncio
async def test_list_jobs_with_search(mock_db, mock_user_id):
    """Test job listing with search query."""
    run_id = "run-456"

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[
                UserJobListRowFactory.build(
                    run_id=run_id,
                    seqera_run_id="wf-456",
                    workflow_type="BindCraft",
                )
            ],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={
                "workflow": {
                    "runName": "Matching Job",
                    "status": "RUNNING",
                }
            },
        ),
    ):
        response = await list_jobs(
            search="matching",
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert len(response.jobs) == 1
    assert response.jobs[0].jobName == "Matching Job"


@pytest.mark.asyncio
async def test_list_jobs_with_status_filter(mock_db, mock_user_id):
    """Test job listing with status filter."""
    run_id = "run-789"

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[
                UserJobListRowFactory.build(run_id=run_id, seqera_run_id="wf-789", score=0.95)
            ],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
    ):
        response = await list_jobs(
            search=None,
            status_filter=["Completed"],
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert len(response.jobs) == 1


@pytest.mark.asyncio
async def test_list_jobs_pending_queued_job_skips_seqera_lookup(test_db, persistent_models):
    """Pending queued jobs are rendered from local DB state without querying Seqera."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    owned_run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id=None,
        binder_name=None,
        run_name="Queued Job",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        tool="colabfold",
    )
    QueuedJobFactory.create_sync(
        workflow_run=owned_run,
        workflow=workflow,
        launch_payload={},
        status="pending",
    )

    describe = AsyncMock()
    with patch("app.routes.workflow.jobs.describe_workflow", describe):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=user.id,
            db=test_db,
        )

    describe.assert_not_awaited()
    assert len(response.jobs) == 1
    assert response.jobs[0].id == str(owned_run.id)
    assert response.jobs[0].jobName == "Queued Job"
    assert response.jobs[0].status == "Pending"


@pytest.mark.asyncio
async def test_list_jobs_failed_queued_job_skips_seqera_lookup(test_db, persistent_models):
    """Failed queued jobs are rendered from local DB state without querying Seqera."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    owned_run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id="wf-should-not-be-read",
        binder_name=None,
        run_name="Failed Queued Job",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        tool="colabfold",
    )
    QueuedJobFactory.create_sync(
        workflow_run=owned_run,
        workflow=workflow,
        launch_payload={},
        status="failed",
        error="Seqera launch failed",
    )

    describe = AsyncMock()
    with patch("app.routes.workflow.jobs.describe_workflow", describe):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=user.id,
            db=test_db,
        )

    describe.assert_not_awaited()
    assert len(response.jobs) == 1
    assert response.jobs[0].id == str(owned_run.id)
    assert response.jobs[0].jobName == "Failed Queued Job"
    assert response.jobs[0].status == "Failed"


@pytest.mark.asyncio
async def test_list_jobs_filters_out_non_matching_status(mock_db, mock_user_id):
    """Test that jobs with non-matching status are filtered out."""
    run_id = "run-999"

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[UserJobListRowFactory.build(run_id=run_id, seqera_run_id="wf-999")],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "RUNNING"}},
        ),
    ):
        response = await list_jobs(
            search=None,
            status_filter=["Completed"],
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert len(response.jobs) == 0


@pytest.mark.asyncio
async def test_list_jobs_with_pagination(mock_db, mock_user_id):
    """Test job listing with pagination."""
    run_ids = [f"run-{i}" for i in range(10)]

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[
                UserJobListRowFactory.build(
                    run_id=run_id,
                    seqera_run_id=f"wf-{index}",
                    score=0.95,
                )
                for index, run_id in enumerate(run_ids)
            ],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=5,
            offset=3,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert response.total == 10
    assert len(response.jobs) == 5
    assert response.limit == 5
    assert response.offset == 3


@pytest.mark.asyncio
async def test_list_jobs_seqera_configuration_error(mock_db, mock_user_id):
    """When Seqera is misconfigured the job list falls back to DB data and flags seqeraUnavailable."""
    from app.services.seqera_errors import SeqeraConfigurationError

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[UserJobListRowFactory.build(run_id="run-1", seqera_run_id="wf-1")],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraConfigurationError("Missing config"),
        ),
    ):
        result = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert result.seqeraUnavailable is True
    assert len(result.jobs) == 1
    assert result.jobs[0].status == "N/A"


@pytest.mark.asyncio
@pytest.mark.parametrize("seqera_status", [403, 404])
async def test_list_jobs_seqera_4xx_skipped(mock_db, mock_user_id, seqera_status):
    """Runs that return 4xx from Seqera are silently skipped (not found, wrong workspace, etc.)."""
    from app.services.seqera_errors import SeqeraAPIError

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[
                UserJobListRowFactory.build(
                    run_id="run-1",
                    seqera_run_id="wf-1",
                )
            ],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("Client error", status_code=seqera_status),
        ),
    ):
        result = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert result.jobs == []


@pytest.mark.asyncio
async def test_list_jobs_seqera_5xx_falls_back(mock_db, mock_user_id):
    """Seqera 5xx errors fall back to DB data and flag seqeraUnavailable instead of surfacing a 502."""
    from app.services.seqera_errors import SeqeraAPIError

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[UserJobListRowFactory.build(run_id="run-1", seqera_run_id="wf-1")],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("Internal error", status_code=500),
        ),
    ):
        result = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert result.seqeraUnavailable is True
    assert len(result.jobs) == 1
    assert result.jobs[0].status == "N/A"


@pytest.mark.asyncio
async def test_get_job_details_success(mock_db, mock_user_id, mocker):
    """Test successful job details retrieval."""
    run_id = "wf-123"
    workflow = mocker.Mock(spec=Workflow)
    workflow.name = "BindCraft"

    owned_run = mocker.Mock(spec=WorkflowRun)
    owned_run.workflow = workflow
    owned_run.tool = None
    owned_run.submitted_form_data = None
    owned_run.seqera_run_id = "seqera-wf-123"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=owned_run),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={
                "workflow": {
                    "runName": "Test Job Details",
                    "status": "SUCCEEDED",
                    "submit": "2026-02-01T10:00:00Z",
                }
            },
        ),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=0.95,
        ),
    ):
        response = await get_job_details(
            run_id=run_id,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert response.id == run_id
    assert response.jobName == "Test Job Details"
    assert response.status == "Completed"
    assert response.workflow == "Bindcraft"
    assert response.score == 0.95


@pytest.mark.asyncio
async def test_get_job_details_not_found(mock_db, mock_user_id):
    """Test job details when job not found."""
    with patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await get_job_details(
                run_id="nonexistent",
                current_user_id=mock_user_id,
                db=mock_db,
            )

    assert exc_info.value.status_code == 404
    assert "Job not found" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_job_details_in_progress_no_score(mock_db, mock_user_id, mocker):
    """Test that in-progress jobs don't return a score."""
    owned_run = mocker.Mock(spec=WorkflowRun)
    owned_run.workflow = None
    owned_run.tool = None
    owned_run.submitted_form_data = None
    owned_run.seqera_run_id = "seqera-wf-456"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=owned_run),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "RUNNING"}},
        ),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=0.95,
        ),
    ):
        response = await get_job_details(
            run_id="wf-456",
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert response.status == "In progress"
    assert response.score is None


@pytest.mark.asyncio
async def test_get_job_details_seqera_error(mock_db, mock_user_id, mocker):
    """Test handling of Seqera API error in job details."""
    from app.services.seqera_errors import SeqeraAPIError

    owned_run = mocker.Mock()
    owned_run.seqera_run_id = "seqera-wf-789"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=owned_run),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("API failed"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_job_details(
                run_id="wf-789",
                current_user_id=mock_user_id,
                db=mock_db,
            )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_list_jobs_with_score_calculation(mock_db, mock_user_id):
    """Test that completed jobs trigger score calculation."""
    run_id = "run-score-test"
    user_run = UserJobListRowFactory.build(run_id=run_id, seqera_run_id="wf-score-test")

    with (
        patch("app.routes.workflow.jobs.get_user_job_list_rows", return_value=[user_run]),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=0.88,
        ) as mock_ensure_score,
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    mock_ensure_score.assert_called_once_with(mock_db, user_run.run, "Completed")
    assert response.jobs[0].score == 0.88
