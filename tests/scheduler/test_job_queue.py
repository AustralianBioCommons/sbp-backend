from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.scheduler import jobs as scheduler_jobs
from app.schemas.backend import ComputeBackendName
from app.services.gadi_pbs_jobs import GadiPbsJobsError, GadiPbsJobsSnapshot, PbsJobStatus
from tests.datagen import (
    AppUserFactory,
    ComputeBackendFactory,
    QueuedJobFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


def _make_scheduler() -> BlockingScheduler:
    return BlockingScheduler(jobstores={"memory": MemoryJobStore()})


def _get_db_override(db):
    def _get_db():
        yield db

    return _get_db


def _create_queued_job(
    *,
    backend,
    status: str = "pending",
    next_attempt_at: datetime | None = None,
    seqera_final_status: str | None = None,
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    workflow_run = WorkflowRunFactory.create_sync(
        owner=user,
        backend=backend,
        seqera_final_status=seqera_final_status,
        workflow=workflow,
        work_dir=f"/work/{status}-{next_attempt_at.timestamp() if next_attempt_at else 'none'}-{uuid4()}",
    )
    return QueuedJobFactory.create_sync(
        workflow=workflow,
        workflow_run=workflow_run,
        launch_payload={},
        status=status,
        next_attempt_at=next_attempt_at,
    )


def test_submit_pending_jobs_skips_when_seqera_unavailable(
    default_backend, test_db, persistent_models, monkeypatch, mock_settings
):
    due_job = _create_queued_job(
        backend=default_backend, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    scheduler = _make_scheduler()
    checked_sessions = []

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(
        scheduler_jobs,
        "is_seqera_available",
        lambda db_session, **_kwargs: checked_sessions.append(db_session) or False,
    )

    scheduler_jobs.submit_pending_jobs()

    assert checked_sessions == [test_db]
    assert scheduler.get_jobs(jobstore="memory") == []

    test_db.refresh(due_job)
    assert due_job.status == "pending"


def test_submit_pending_jobs_schedules_only_due_pending_jobs(
    default_backend, test_db, persistent_models, monkeypatch
):
    now = datetime.now(UTC)
    due_pending_job = _create_queued_job(
        backend=default_backend, status="pending", next_attempt_at=now - timedelta(minutes=1)
    )
    _create_queued_job(
        backend=default_backend, status="pending", next_attempt_at=now + timedelta(minutes=1)
    )
    _create_queued_job(
        backend=default_backend, status="submitted", next_attempt_at=now - timedelta(minutes=1)
    )
    _create_queued_job(
        backend=default_backend, status="failed", next_attempt_at=now - timedelta(minutes=1)
    )
    _create_queued_job(backend=default_backend, status="pending", next_attempt_at=None)
    scheduler = _make_scheduler()

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_jobs,
        "get_available_workflow_capacity",
        lambda _db_session, **_kwargs: 25,
    )

    scheduler_jobs.submit_pending_jobs(dry_run=True)

    launch_id = f"launch_job_{due_pending_job.id}"
    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert [job.id for job in scheduled_jobs] == [launch_id]
    scheduled_job = scheduled_jobs[0]
    assert scheduled_job.func is scheduler_jobs.launch_job
    assert scheduled_job.kwargs == {"job_id": due_pending_job.id, "dry_run": True}
    assert scheduled_job.name == launch_id
    assert scheduled_job.max_instances == 1


def test_submit_pending_jobs_skips_jobs_already_scheduled(
    default_backend, test_db, persistent_models, monkeypatch
):
    due_job = _create_queued_job(
        backend=default_backend, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    launch_id = f"launch_job_{due_job.id}"
    scheduler = _make_scheduler()
    scheduler.add_job(
        scheduler_jobs.launch_job,
        id=launch_id,
        jobstore="memory",
        kwargs={"job_id": due_job.id, "dry_run": False},
        name=launch_id,
        max_instances=1,
        replace_existing=True,
    )

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_jobs,
        "get_available_workflow_capacity",
        lambda _db_session, **_kwargs: 25,
    )

    scheduler_jobs.submit_pending_jobs()

    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert [job.id for job in scheduled_jobs] == [launch_id]
    assert scheduled_jobs[0].kwargs == {"job_id": due_job.id, "dry_run": False}
    test_db.refresh(due_job)
    assert due_job.status == "launching"
    assert due_job.next_attempt_at is not None


def test_submit_pending_jobs_skips_when_no_gadi_capacity(
    default_backend, test_db, persistent_models, monkeypatch
):
    _create_queued_job(
        backend=default_backend, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    scheduler = _make_scheduler()

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_jobs, "get_available_workflow_capacity", lambda _db_session, **_kwargs: 0
    )

    scheduler_jobs.submit_pending_jobs()

    assert scheduler.get_jobs(jobstore="memory") == []


def test_submit_pending_jobs_caps_submissions_to_available_capacity(
    default_backend, test_db, persistent_models, monkeypatch
):
    now = datetime.now(UTC)
    due_jobs = [
        _create_queued_job(
            backend=default_backend, status="pending", next_attempt_at=now - timedelta(minutes=1)
        )
        for _ in range(3)
    ]
    scheduler = _make_scheduler()

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_jobs, "get_available_workflow_capacity", lambda _db_session, **_kwargs: 2
    )

    scheduler_jobs.submit_pending_jobs()

    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert len(scheduled_jobs) == 2
    scheduled_job_ids = {job.kwargs["job_id"] for job in scheduled_jobs}
    assert scheduled_job_ids.issubset({job.id for job in due_jobs})
    for job in due_jobs:
        test_db.refresh(job)
    assert sum(1 for job in due_jobs if job.status == "launching") == 2
    assert sum(1 for job in due_jobs if job.status == "pending") == 1


def test_submit_pending_jobs_reschedules_due_launching_jobs(
    default_backend, test_db, persistent_models, monkeypatch
):
    due_launching_job = _create_queued_job(
        backend=default_backend,
        status="launching",
        next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    scheduler = _make_scheduler()

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_jobs, "get_available_workflow_capacity", lambda _db_session, **_kwargs: 1
    )

    scheduler_jobs.submit_pending_jobs()

    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert [job.kwargs["job_id"] for job in scheduled_jobs] == [due_launching_job.id]
    test_db.refresh(due_launching_job)
    assert due_launching_job.status == "launching"
    assert due_launching_job.next_attempt_at is not None


def _pbs_job(job_id: str, *, queue: str, state: str) -> PbsJobStatus:
    return PbsJobStatus(
        job_id=job_id,
        job_name=None,
        state=state,
        state_label=state,
        queue=queue,
        account=None,
        submitted_at=None,
        started_at=None,
    )


def _patch_pbs_snapshot(monkeypatch, jobs: list[PbsJobStatus], generated_at: datetime):
    async def _get_pbs_jobs(**_kwargs):
        return GadiPbsJobsSnapshot(generated_at=generated_at, jobs=jobs, queue_totals=[])

    monkeypatch.setattr(scheduler_jobs.gadi_pbs_jobs, "get_pbs_jobs", _get_pbs_jobs)


def test_get_available_workflow_capacity_counts_queued_and_running_workflow_exec_jobs(
    default_backend, test_db, monkeypatch, mock_settings
):
    _patch_pbs_snapshot(
        monkeypatch,
        [
            _pbs_job("1", queue="workflow-exec", state="R"),
            _pbs_job("2", queue="workflow-exec", state="R"),
            _pbs_job("3", queue="workflow-exec", state="Q"),
            # Also counted: the "workflow" route queue.
            _pbs_job("4", queue="workflow", state="Q"),
            # Not counted: other states / other queues.
            _pbs_job("5", queue="workflow-exec", state="H"),
            _pbs_job("6", queue="workflow-exec", state="F"),
            _pbs_job("7", queue="normal", state="R"),
            _pbs_job("8", queue="gpuhopper", state="Q"),
        ],
        generated_at=datetime.now(UTC),
    )
    default_backend.max_workflows = 10

    assert (
        scheduler_jobs.get_available_workflow_capacity(
            test_db, default_backend, settings=mock_settings
        )
        == 6
    )


def test_get_available_workflow_capacity_counts_jobs_submitted_after_snapshot(
    default_backend, test_db, persistent_models, monkeypatch, mock_settings
):
    generated_at = datetime.now(UTC) - timedelta(minutes=10)
    before = _create_queued_job(backend=default_backend, status="submitted")
    before.submitted_at = generated_at - timedelta(minutes=1)
    after = _create_queued_job(backend=default_backend, status="submitted")
    after.submitted_at = generated_at + timedelta(minutes=1)
    other_backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    other = _create_queued_job(backend=other_backend, status="submitted")
    other.submitted_at = generated_at + timedelta(minutes=1)
    test_db.add_all([before, after, other])
    test_db.commit()
    _patch_pbs_snapshot(
        monkeypatch, [_pbs_job("1", queue="workflow-exec", state="R")], generated_at
    )
    default_backend.max_workflows = 10

    # 1 in the snapshot + 1 submitted since it was generated.
    assert (
        scheduler_jobs.get_available_workflow_capacity(
            test_db, default_backend, settings=mock_settings
        )
        == 8
    )


def test_get_available_workflow_capacity_falls_back_to_seqera_for_stale_snapshot(
    default_backend, test_db, monkeypatch, mock_settings
):
    """A stuck snapshot must fall back to Seqera's live count, not a rolling
    window that would fail open unboundedly the longer it stays stuck."""
    generated_at = datetime.now(UTC) - scheduler_jobs.STALE_PBS_SNAPSHOT_TIMEOUT - timedelta(days=1)
    _patch_pbs_snapshot(
        monkeypatch,
        # Would otherwise count for 5 - must be ignored once stale.
        [_pbs_job(str(i), queue="workflow-exec", state="R") for i in range(5)],
        generated_at,
    )

    async def _count_active_workflows(*, workspace_id, settings):
        assert workspace_id == default_backend.seqera_workspace_id
        assert settings is mock_settings
        return 3

    monkeypatch.setattr(scheduler_jobs.seqera, "count_active_workflows", _count_active_workflows)
    default_backend.max_workflows = 10

    assert (
        scheduler_jobs.get_available_workflow_capacity(
            test_db, default_backend, settings=mock_settings
        )
        == 7
    )


def test_get_available_workflow_capacity_floors_at_zero(
    default_backend, test_db, monkeypatch, mock_settings
):
    _patch_pbs_snapshot(
        monkeypatch,
        [_pbs_job(str(i), queue="workflow-exec", state="Q") for i in range(12)],
        generated_at=datetime.now(UTC),
    )
    default_backend.max_workflows = 10

    assert (
        scheduler_jobs.get_available_workflow_capacity(
            test_db, default_backend, settings=mock_settings
        )
        == 0
    )


def test_submit_pending_jobs_skips_when_pbs_snapshot_unavailable(
    default_backend, test_db, persistent_models, monkeypatch
):
    _create_queued_job(
        backend=default_backend, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    scheduler = _make_scheduler()

    default_backend.max_workflows = 10

    async def _get_pbs_jobs(**_kwargs):
        raise GadiPbsJobsError("missing")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs.gadi_pbs_jobs, "get_pbs_jobs", _get_pbs_jobs)

    scheduler_jobs.submit_pending_jobs()

    assert scheduler.get_jobs(jobstore="memory") == []


@pytest.mark.parametrize("limit", [None, 0, 1, 5])
def test_setonix_capacity_counts_only_its_submitted_unfinished_runs(
    test_db, default_backend, mock_settings, mocker, limit
):
    backend = ComputeBackendFactory.create_sync(
        name=ComputeBackendName.SETONIX, max_workflows=limit
    )
    for status in [None, "RUNNING", "SUBMITTED", "SUCCEEDED", "failed", "CANCELLED", "UNKNOWN"]:
        _create_queued_job(backend=backend, status="submitted", seqera_final_status=status)
    for status in ["pending", "staging", "launching", "failed", "cancelled"]:
        _create_queued_job(backend=backend, status=status)
    _create_queued_job(backend=default_backend, status="submitted")
    pbs = mocker.patch.object(scheduler_jobs.gadi_pbs_jobs, "get_pbs_jobs")

    capacity = scheduler_jobs.get_available_workflow_capacity(
        test_db, backend, settings=mock_settings
    )

    assert capacity == (None if limit is None else max(0, limit - 3))
    pbs.assert_not_called()


def test_uncapped_gadi_does_not_require_pbs_snapshot(test_db, default_backend, mocker):
    default_backend.max_workflows = None
    pbs = mocker.patch.object(scheduler_jobs.gadi_pbs_jobs, "get_pbs_jobs")

    assert scheduler_jobs.get_available_workflow_capacity(test_db, default_backend) is None
    pbs.assert_not_called()


@pytest.mark.parametrize("gadi_capacity", [0, 1, "unavailable"])
@pytest.mark.parametrize("setonix_limit", [2, None])
def test_submission_applies_each_backends_capacity_independently(
    test_db, default_backend, monkeypatch, mocker, gadi_capacity, setonix_limit
):
    backend = ComputeBackendFactory.create_sync(
        name=ComputeBackendName.SETONIX, max_workflows=setonix_limit
    )
    due = datetime.now(UTC) - timedelta(minutes=1)
    gadi_jobs = [_create_queued_job(backend=default_backend, next_attempt_at=due) for _ in range(2)]
    setonix_jobs = [_create_queued_job(backend=backend, next_attempt_at=due) for _ in range(3)]
    _create_queued_job(backend=backend, status="submitted")
    scheduler = _make_scheduler()
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda *_args, **_kwargs: True)
    check = mocker.Mock(
        side_effect=GadiPbsJobsError("missing") if gadi_capacity == "unavailable" else None,
        return_value=gadi_capacity,
    )
    monkeypatch.setitem(scheduler_jobs.WORKFLOW_CAPACITY_CHECKS, ComputeBackendName.GADI, check)

    scheduler_jobs.submit_pending_jobs(db_session=test_db)

    scheduled = {job.kwargs["job_id"] for job in scheduler.get_jobs(jobstore="memory")}
    expected_gadi = 0 if gadi_capacity == "unavailable" else gadi_capacity
    expected_setonix = 3 if setonix_limit is None else 1
    assert len(scheduled & {job.id for job in gadi_jobs}) == expected_gadi
    assert len(scheduled & {job.id for job in setonix_jobs}) == expected_setonix
    assert check.call_count == 1
    for job in gadi_jobs + setonix_jobs:
        test_db.refresh(job)
        assert job.status == ("launching" if job.id in scheduled else "pending")
