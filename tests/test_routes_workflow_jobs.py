"""Tests for job listing and details endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.models.core import AppUser, Workflow, WorkflowRun
from app.main import create_app
from app.routes.workflow.jobs import get_job_details, list_jobs


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
    run_id = "wf-123"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=[run_id]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id",
            return_value={run_id: "BindCraft"},
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={
                "workflow": {
                    "id": run_id,
                    "runName": "Test Job",
                    "status": "SUCCEEDED",
                    "submit": "2026-02-01T10:00:00Z",
                }
            },
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=None),
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
    assert response.jobs[0].workflowType == "BindCraft"


@pytest.mark.asyncio
async def test_list_jobs_with_search(mock_db, mock_user_id):
    """Test job listing with search query."""
    run_id = "wf-456"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=[run_id]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id",
            return_value={run_id: "BindCraft"},
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
        patch("app.routes.workflow.jobs.get_owned_run", return_value=None),
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
    run_id = "wf-789"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=[run_id]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=None),
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
async def test_list_jobs_filters_out_non_matching_status(mock_db, mock_user_id):
    """Test that jobs with non-matching status are filtered out."""
    run_id = "wf-999"

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=[run_id]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "RUNNING"}},
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=None),
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
    run_ids = [f"wf-{i}" for i in range(10)]

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=run_ids),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=None),
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
    """Test handling of Seqera configuration error."""
    from app.services.seqera import SeqeraConfigurationError

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=["wf-1"]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraConfigurationError("Missing config"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await list_jobs(
                search=None,
                status_filter=None,
                limit=50,
                offset=0,
                current_user_id=mock_user_id,
                db=mock_db,
            )

    assert exc_info.value.status_code == 500
    assert "Missing config" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_list_jobs_seqera_api_error(mock_db, mock_user_id):
    """Test handling of Seqera API error."""
    from app.services.seqera import SeqeraAPIError

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=["wf-1"]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("API error"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await list_jobs(
                search=None,
                status_filter=None,
                limit=50,
                offset=0,
                current_user_id=mock_user_id,
                db=mock_db,
            )

    assert exc_info.value.status_code == 502
    assert "API error" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_job_details_success(mock_db, mock_user_id, mocker):
    """Test successful job details retrieval."""
    run_id = "wf-123"
    workflow = mocker.Mock(spec=Workflow)
    workflow.name = "BindCraft"

    owned_run = mocker.Mock(spec=WorkflowRun)
    owned_run.workflow = workflow

    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
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
    assert response.workflowType == "BindCraft"
    assert response.score == 0.95


@pytest.mark.asyncio
async def test_get_job_details_not_found(mock_db, mock_user_id):
    """Test job details when job not found."""
    with patch("app.routes.workflow.jobs.get_owned_run", return_value=None):
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

    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
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
    from app.services.seqera import SeqeraAPIError

    owned_run = mocker.Mock()

    with (
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
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
async def test_list_jobs_with_score_calculation(mock_db, mock_user_id, mocker):
    """Test that completed jobs trigger score calculation."""
    run_id = "wf-score-test"
    owned_run = mocker.Mock()

    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value=[run_id]),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_workflow_type_by_seqera_run_id", return_value={}),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=owned_run),
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

    mock_ensure_score.assert_called_once()
    assert response.jobs[0].score == 0.88
