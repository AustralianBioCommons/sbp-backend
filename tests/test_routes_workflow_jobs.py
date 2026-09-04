"""Tests for job listing and details endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.models.core import RunMetric
from app.main import create_app
from app.routes.workflow.jobs import get_job_details, list_jobs
from app.services.job_utils import UserJobListRow
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

    row = UserJobListRowFactory.build(
        run_id=run_id,
        seqera_run_id=seqera_run_id,
        workflow_type="BindCraft",
        tool="BindCraft",
        score=0.95,
    )
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
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
async def test_list_jobs_search_path_sorts_and_filters(mock_db, mock_user_id):
    """The full-scan (search) fallback path still sorts and filters correctly -
    exercises _compare_jobs/_compare_submitted directly (score ties, nulls, both
    sort_by branches), not just the DB-sorted fast path."""

    def scored_row(run_id: str, score: float | None, submitted_at: datetime) -> UserJobListRow:
        run = WorkflowRunFactory.build(
            seqera_final_status="SUCCEEDED" if score is not None else "FAILED",
            sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
            binder_name=None,
            run_name=None,
            submission_timestamp=submitted_at,
        )
        return UserJobListRowFactory.build(
            run=run,
            run_id=run_id,
            seqera_run_id=f"wf-{run_id}",
            workflow_type="Job Alpha",
            score=score,
        )

    excluded = UserJobListRowFactory.build(
        run=WorkflowRunFactory.build(
            seqera_final_status="SUCCEEDED",
            sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
            binder_name=None,
            run_name=None,
            submission_timestamp=datetime(2026, 2, 1, 6, 0, tzinfo=UTC),
        ),
        run_id="excluded",
        seqera_run_id="wf-excluded",
        workflow_type="Other Thing",
        score=0.3,
    )
    rows = [
        scored_row("tie-a", 0.5, datetime(2026, 2, 1, 9, 0, tzinfo=UTC)),
        scored_row("tie-b", 0.5, datetime(2026, 2, 1, 10, 0, tzinfo=UTC)),
        scored_row("high", 0.9, datetime(2026, 2, 1, 8, 0, tzinfo=UTC)),
        scored_row("failed", None, datetime(2026, 2, 1, 7, 0, tzinfo=UTC)),
        excluded,
    ]

    describe = AsyncMock()
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_rows", return_value=rows),
        patch("app.routes.workflow.jobs.describe_workflow", describe),
    ):
        desc = await list_jobs(
            search="job",
            status_filter=None,
            limit=50,
            offset=0,
            sort_by="score",
            sort_order="desc",
            current_user_id=mock_user_id,
            db=mock_db,
        )
        asc = await list_jobs(
            search="job",
            status_filter=None,
            limit=50,
            offset=0,
            sort_by="score",
            sort_order="asc",
            current_user_id=mock_user_id,
            db=mock_db,
        )
        by_submitted = await list_jobs(
            search="job",
            status_filter=None,
            limit=50,
            offset=0,
            sort_by="submitted",
            sort_order="asc",
            current_user_id=mock_user_id,
            db=mock_db,
        )

    describe.assert_not_awaited()
    # "excluded" never matches the search text - its workflow/tool/name don't contain "job".
    assert [job.id for job in desc.jobs] == ["high", "tie-b", "tie-a", "failed"]
    ids_asc = [job.id for job in asc.jobs]
    assert ids_asc[:3] == ["tie-b", "tie-a", "high"]
    assert ids_asc[3] == "failed"
    assert [job.id for job in by_submitted.jobs] == ["failed", "high", "tie-a", "tie-b"]


@pytest.mark.asyncio
async def test_list_jobs_search_path_skips_4xx_inaccessible_run(mock_db, mock_user_id):
    """4xx-inaccessible runs are dropped in the full-scan (search) path too, not just
    the DB-paginated fast path."""
    from app.services.seqera_errors import SeqeraAPIError

    visible = UserJobListRowFactory.build(
        run=WorkflowRunFactory.build(
            seqera_final_status="SUCCEEDED", binder_name=None, run_name=None
        ),
        run_id="visible",
        seqera_run_id="wf-visible",
        workflow_type="Job Alpha",
    )
    inaccessible = UserJobListRowFactory.build(
        run=WorkflowRunFactory.build(seqera_final_status=None, binder_name=None, run_name=None),
        run_id="gone",
        seqera_run_id="wf-gone",
        workflow_type="Job Alpha",
    )

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[visible, inaccessible],
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=SeqeraAPIError("Not found", status_code=404),
        ),
    ):
        response = await list_jobs(
            search="job",
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert [job.id for job in response.jobs] == ["visible"]


@pytest.mark.asyncio
async def test_list_jobs_live_only_status_filter_selects_matching_rows(mock_db, mock_user_id):
    """Filtering to a live-only status (In progress) uses the full-scan path and still
    filters out rows that resolve to a different live status (here, In queue)."""
    running = UserJobListRowFactory.build(
        run=WorkflowRunFactory.build(seqera_final_status=None, binder_name=None, run_name=None),
        run_id="running",
        seqera_run_id="wf-running",
    )
    queued = UserJobListRowFactory.build(
        run=WorkflowRunFactory.build(seqera_final_status=None, binder_name=None, run_name=None),
        run_id="queued",
        seqera_run_id="wf-queued",
    )

    async def fake_describe(seqera_run_id, settings=None):
        if seqera_run_id == "wf-running":
            return {"workflow": {"status": "RUNNING"}}
        return {"workflow": {"status": "SUBMITTED"}}

    with (
        patch(
            "app.routes.workflow.jobs.get_user_job_list_rows",
            return_value=[running, queued],
        ),
        patch("app.routes.workflow.jobs.describe_workflow", side_effect=fake_describe),
    ):
        response = await list_jobs(
            search=None,
            status_filter=["In progress"],
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    assert [job.id for job in response.jobs] == ["running"]


@pytest.mark.asyncio
async def test_list_jobs_seqera_unexpected_error_falls_back(mock_db, mock_user_id):
    """A non-SeqeraAPIError exception from describe_workflow still falls back to DB
    data and flags seqeraUnavailable, same as a 5xx SeqeraAPIError."""
    row = UserJobListRowFactory.build(run_id="run-1", seqera_run_id="wf-1")
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
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


@pytest.mark.asyncio
async def test_list_jobs_with_status_filter(mock_db, mock_user_id):
    """Test job listing with status filter."""
    run_id = "run-789"

    row = UserJobListRowFactory.build(run_id=run_id, seqera_run_id="wf-789", score=0.95)
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
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
async def test_list_jobs_staging_queued_job_skips_seqera_lookup(test_db, persistent_models):
    """Staging queued jobs (Globus input data transfer still in flight) are
    rendered as "Staging", not "Pending" or a Seqera lookup that has no run yet."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    owned_run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id=None,
        binder_name=None,
        run_name="Staging Job",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        tool="colabfold",
    )
    QueuedJobFactory.create_sync(
        workflow_run=owned_run,
        workflow=workflow,
        launch_payload={},
        status="staging",
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
    assert response.jobs[0].jobName == "Staging Job"
    assert response.jobs[0].status == "Staging"


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
async def test_list_jobs_stored_terminal_status_skips_seqera_lookup(mock_db, mock_user_id):
    """Test seqera lookup is skipped when seqera_final_status is recorded"""
    run = WorkflowRunFactory.build(
        seqera_run_id="wf-cached-complete",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        binder_name=None,
        run_name="Cached Complete Job",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )
    run.metrics = None
    user_run = UserJobListRowFactory.build(
        run=run,
        run_id="run-cached-complete",
        seqera_run_id="wf-cached-complete",
        workflow_type="Single Prediction",
        score=0.91,
    )
    describe = AsyncMock()

    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([user_run], 1)),
        patch("app.routes.workflow.jobs.describe_workflow", describe),
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    describe.assert_not_awaited()
    assert len(response.jobs) == 1
    assert response.jobs[0].jobName == "Cached Complete Job"
    assert response.jobs[0].status == "Completed"
    assert response.jobs[0].score == 0.91


@pytest.mark.asyncio
async def test_list_jobs_synced_completed_run_skips_score_and_usage_sync(mock_db, mock_user_id):
    """Completed runs already marked synced don't sync score and usage"""
    run = WorkflowRunFactory.build(
        seqera_run_id="wf-synced-complete",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        service_usage=None,
        binder_name=None,
        run_name="Synced Complete Job",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )
    run.metrics = None
    user_run = UserJobListRowFactory.build(
        run=run,
        run_id="run-synced-complete",
        seqera_run_id="wf-synced-complete",
        score=None,
    )

    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([user_run], 1)),
        patch("app.routes.workflow.jobs.describe_workflow", new_callable=AsyncMock) as describe,
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    describe.assert_not_awaited()
    assert len(response.jobs) == 1
    assert response.jobs[0].status == "Completed"
    assert response.jobs[0].score is None


@pytest.mark.asyncio
async def test_list_jobs_filters_out_non_matching_status(mock_db, mock_user_id):
    """Test that jobs with non-matching status are filtered out."""
    run_id = "run-999"

    row = UserJobListRowFactory.build(run_id=run_id, seqera_run_id="wf-999")
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
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
async def test_list_jobs_with_pagination(test_db, persistent_models):
    """Test job listing with pagination, sorted/sliced by the DB query."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    for index in range(10):
        run = WorkflowRunFactory.create_sync(
            workflow=workflow,
            owner=user,
            seqera_run_id=f"wf-{index}",
            seqera_final_status="SUCCEEDED",
            sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
            binder_name=None,
            run_name=f"run-{index}",
            submission_timestamp=datetime(2026, 2, 1, 10, index, tzinfo=UTC),
        )
        test_db.add(RunMetric(run_id=run.id, max_score=0.95))
    test_db.commit()

    describe = AsyncMock()
    with patch("app.routes.workflow.jobs.describe_workflow", describe):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=5,
            offset=3,
            current_user_id=user.id,
            db=test_db,
        )

    describe.assert_not_awaited()
    assert response.total == 10
    assert len(response.jobs) == 5
    assert response.limit == 5
    assert response.offset == 3


@pytest.mark.asyncio
async def test_list_jobs_sort_by_score_places_failed_jobs_last(test_db, persistent_models):
    """Unscored (e.g. failed) jobs sort to the end in both directions, even across pages.
    Exercises the DB-level `ORDER BY ... NULLS LAST`, not just the search/live-status
    fallback path."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")

    def make_run(name: str, score: float | None, *, failed: bool = False) -> None:
        run = WorkflowRunFactory.create_sync(
            workflow=workflow,
            owner=user,
            seqera_run_id=None if failed else f"wf-{name}",
            seqera_final_status="FAILED" if failed else "SUCCEEDED",
            sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
            binder_name=None,
            run_name=name,
            submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        )
        if score is not None:
            test_db.add(RunMetric(run_id=run.id, max_score=score))

    make_run("mid", 0.5)
    make_run("failed-1", None, failed=True)
    make_run("high", 0.9)
    make_run("failed-2", None, failed=True)
    make_run("low", 0.1)
    test_db.commit()

    describe = AsyncMock()
    with patch("app.routes.workflow.jobs.describe_workflow", describe):
        desc_page_1 = await list_jobs(
            search=None,
            status_filter=None,
            limit=2,
            offset=0,
            sort_by="score",
            sort_order="desc",
            current_user_id=user.id,
            db=test_db,
        )
        asc_all = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            sort_by="score",
            sort_order="asc",
            current_user_id=user.id,
            db=test_db,
        )

    describe.assert_not_awaited()
    # The first (highest-score) page must not contain any failed/unscored jobs.
    assert [job.jobName for job in desc_page_1.jobs] == ["high", "mid"]
    # Ascending order still pushes failed jobs (order between them is unspecified) to the very end.
    names = [job.jobName for job in asc_all.jobs]
    assert names[:3] == ["low", "mid", "high"]
    assert set(names[3:]) == {"failed-1", "failed-2"}


@pytest.mark.asyncio
async def test_list_jobs_status_filter_db_resolvable_combo(test_db, persistent_models):
    """Completed/Failed/Stopped/Pending/Staging are all resolvable from stored columns,
    so combining them stays a single DB query with no live Seqera calls."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")

    def make_terminal(name: str, seqera_final_status: str) -> None:
        WorkflowRunFactory.create_sync(
            workflow=workflow,
            owner=user,
            seqera_run_id=f"wf-{name}",
            seqera_final_status=seqera_final_status,
            sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
            binder_name=None,
            run_name=name,
            submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        )

    make_terminal("done", "SUCCEEDED")
    make_terminal("broke", "FAILED")
    make_terminal("axed", "CANCELLED")

    pending_run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id=None,
        binder_name=None,
        run_name="queued",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )
    QueuedJobFactory.create_sync(
        workflow_run=pending_run,
        workflow=workflow,
        launch_payload={},
        status="pending",
    )
    test_db.commit()

    describe = AsyncMock()
    with patch("app.routes.workflow.jobs.describe_workflow", describe):
        response = await list_jobs(
            search=None,
            status_filter=["Failed", "Stopped", "Pending"],
            limit=50,
            offset=0,
            current_user_id=user.id,
            db=test_db,
        )

    describe.assert_not_awaited()
    assert {job.jobName for job in response.jobs} == {"broke", "axed", "queued"}


@pytest.mark.asyncio
async def test_list_jobs_completed_filter_matches_unresolved_scored_row_via_db(
    test_db, persistent_models
):
    """A row with a cached score but no terminal Seqera status yet (e.g. Seqera was
    unreachable on a prior sync) should still match a Completed filter - the DB query
    treats "not locally queued, not terminal, but scored" as Completed."""
    from app.services.seqera_errors import SeqeraAPIError

    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id="wf-ambiguous",
        seqera_final_status=None,
        sync_completed_at=None,
        binder_name=None,
        run_name="ambiguous",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )
    test_db.add(RunMetric(run_id=run.id, max_score=0.7))
    test_db.commit()

    with patch(
        "app.routes.workflow.jobs.describe_workflow",
        new_callable=AsyncMock,
        side_effect=SeqeraAPIError("Internal error", status_code=500),
    ):
        response = await list_jobs(
            search=None,
            status_filter=["Completed"],
            limit=50,
            offset=0,
            current_user_id=user.id,
            db=test_db,
        )

    assert len(response.jobs) == 1
    assert response.jobs[0].status == "Completed"
    assert response.jobs[0].score == 0.7
    assert response.seqeraUnavailable is True


@pytest.mark.asyncio
async def test_list_jobs_completed_filter_drops_scored_row_that_is_actually_still_running(
    test_db, persistent_models
):
    """The DB query optimistically includes a scored-but-not-yet-terminal row under a
    Completed filter; if the live Seqera check (done for the returned page) reveals
    it's actually still running, it must be dropped rather than misreported."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    run = WorkflowRunFactory.create_sync(
        workflow=workflow,
        owner=user,
        seqera_run_id="wf-ambiguous",
        seqera_final_status=None,
        sync_completed_at=None,
        binder_name=None,
        run_name="ambiguous",
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )
    test_db.add(RunMetric(run_id=run.id, max_score=0.7))
    test_db.commit()

    with patch(
        "app.routes.workflow.jobs.describe_workflow",
        new_callable=AsyncMock,
        return_value={"workflow": {"status": "RUNNING"}},
    ):
        response = await list_jobs(
            search=None,
            status_filter=["Completed"],
            limit=50,
            offset=0,
            current_user_id=user.id,
            db=test_db,
        )

    assert response.jobs == []


@pytest.mark.asyncio
async def test_list_jobs_routes_search_to_full_scan_not_db_page(mock_db, mock_user_id):
    """A search term can't be resolved by the DB query alone (a job name can fall back
    to a live Seqera value), so it must use the full-scan path, not the DB-paginated
    one."""
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_rows", return_value=[]) as get_rows,
        patch("app.routes.workflow.jobs.get_user_job_list_page") as get_page,
    ):
        await list_jobs(
            search="anything",
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    get_rows.assert_called_once()
    get_page.assert_not_called()


@pytest.mark.asyncio
async def test_list_jobs_routes_live_only_status_filter_to_full_scan(mock_db, mock_user_id):
    """In queue / In progress aren't persisted anywhere (see LIVE_ONLY_UI_STATUSES), so
    filtering on them must use the full-scan path, not the DB-paginated one."""
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_rows", return_value=[]) as get_rows,
        patch("app.routes.workflow.jobs.get_user_job_list_page") as get_page,
    ):
        await list_jobs(
            search=None,
            status_filter=["In progress"],
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    get_rows.assert_called_once()
    get_page.assert_not_called()


@pytest.mark.asyncio
async def test_list_jobs_routes_plain_listing_to_db_page(mock_db, mock_user_id):
    """No search and only DB-resolvable statuses should use the DB-paginated path."""
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_rows") as get_rows,
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([], 0)) as get_page,
    ):
        await list_jobs(
            search=None,
            status_filter=["Completed"],
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    get_page.assert_called_once()
    get_rows.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("seqera_status", [403, 404])
async def test_list_jobs_seqera_4xx_skipped(mock_db, mock_user_id, seqera_status):
    """Runs that return 4xx from Seqera are silently skipped (not found, wrong workspace, etc.)."""
    from app.services.seqera_errors import SeqeraAPIError

    row = UserJobListRowFactory.build(run_id="run-1", seqera_run_id="wf-1")
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
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

    row = UserJobListRowFactory.build(run_id="run-1", seqera_run_id="wf-1")
    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([row], 1)),
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
async def test_get_job_details_success(mock_db, mock_user_id):
    """Test successful job details retrieval."""
    run_id = "wf-123"
    workflow = WorkflowFactory.build(name="BindCraft")
    owned_run = WorkflowRunFactory.build(
        workflow=workflow,
        seqera_run_id="seqera-wf-123",
        seqera_final_status=None,
        sync_completed_at=None,
        service_usage=1.0,
        binder_name=None,
        run_name="Test Job Details",
    )
    owned_run.metrics = None

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
    assert response.score is None


@pytest.mark.asyncio
async def test_get_job_details_uses_stored_terminal_status_without_seqera(mock_db, mock_user_id):
    """Completed synced jobs are rendered from DB state without querying Seqera or syncing."""
    run_id = "wf-cached"
    workflow = WorkflowFactory.build(name="single-prediction")
    owned_run = WorkflowRunFactory.build(
        workflow=workflow,
        tool="boltz",
        submitted_form_data=None,
        seqera_run_id="seqera-wf-cached",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        service_usage=2.5,
        submission_timestamp=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        binder_name=None,
        run_name="Cached Job Details",
    )
    owned_run.metrics = RunMetric(max_score=0.875)

    with (
        patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=owned_run),
        patch("app.routes.workflow.jobs.describe_workflow", new_callable=AsyncMock) as describe,
    ):
        response = await get_job_details(
            run_id=run_id,
            current_user_id=mock_user_id,
            db=mock_db,
        )

    describe.assert_not_awaited()
    assert response.id == run_id
    assert response.jobName == "Cached Job Details"
    assert response.status == "Completed"
    assert response.workflow == "Single Prediction"
    assert response.tool == "Boltz"
    assert response.score == 0.875
    assert response.submittedAt == datetime(2026, 2, 1, 10, 0, tzinfo=UTC)


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
async def test_get_job_details_in_progress_no_score(mock_db, mock_user_id):
    """Test that in-progress jobs don't return a score."""
    owned_run = WorkflowRunFactory.build(
        workflow=None,
        tool=None,
        submitted_form_data=None,
        seqera_run_id="seqera-wf-456",
        seqera_final_status=None,
        sync_completed_at=None,
        service_usage=None,
        submission_timestamp=None,
    )
    owned_run.metrics = None

    with (
        patch("app.routes.workflow.jobs.get_owned_run_by_id", return_value=owned_run),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "RUNNING"}},
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
async def test_get_job_details_seqera_error(mock_db, mock_user_id):
    """Test handling of Seqera API error in job details."""
    from app.services.seqera_errors import SeqeraAPIError

    owned_run = WorkflowRunFactory.build(
        seqera_run_id="seqera-wf-789",
        seqera_final_status=None,
    )

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
async def test_list_jobs_does_not_calculate_score_before_result_sync(
    mock_db, mock_user_id, mock_settings
):
    """Completed compute does not trigger request-time S3 result discovery."""
    run_id = "run-score-test"
    run = WorkflowRunFactory.build(
        seqera_run_id="wf-score-test",
        seqera_final_status=None,
        sync_completed_at=None,
        submitted_form_data=None,
    )
    run.metrics = None
    user_run = UserJobListRowFactory.build(
        run=run,
        run_id=run_id,
        seqera_run_id="wf-score-test",
    )

    with (
        patch("app.routes.workflow.jobs.get_user_job_list_page", return_value=([user_run], 1)),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            return_value={"workflow": {"status": "SUCCEEDED"}},
        ),
    ):
        response = await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            current_user_id=mock_user_id,
            db=mock_db,
            settings=mock_settings,
        )

    assert response.jobs[0].score is None
