from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.scheduler import jobs as scheduler_jobs
from app.services.gadi_pbs_jobs import GadiPbsJobsError, GadiPbsJobsSnapshot, PbsJobStatus
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory


def _make_scheduler() -> BlockingScheduler:
    return BlockingScheduler(jobstores={"memory": MemoryJobStore()})


def _get_db_override(db):
    def _get_db():
        yield db

    return _get_db


def _create_queued_job(*, status: str = "pending", next_attempt_at: datetime | None = None):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    workflow_run = WorkflowRunFactory.create_sync(
        owner=user,
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
    test_db, persistent_models, monkeypatch, mock_settings
):
    due_job = _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
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
    test_db, persistent_models, monkeypatch
):
    now = datetime.now(UTC)
    due_pending_job = _create_queued_job(
        status="pending", next_attempt_at=now - timedelta(minutes=1)
    )
    _create_queued_job(status="pending", next_attempt_at=now + timedelta(minutes=1))
    _create_queued_job(status="submitted", next_attempt_at=now - timedelta(minutes=1))
    _create_queued_job(status="failed", next_attempt_at=now - timedelta(minutes=1))
    _create_queued_job(status="pending", next_attempt_at=None)
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


def test_submit_pending_jobs_skips_jobs_already_scheduled(test_db, persistent_models, monkeypatch):
    due_job = _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
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


def test_submit_pending_jobs_skips_when_no_gadi_capacity(test_db, persistent_models, monkeypatch):
    _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
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
    test_db, persistent_models, monkeypatch
):
    now = datetime.now(UTC)
    due_jobs = [
        _create_queued_job(status="pending", next_attempt_at=now - timedelta(minutes=1))
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
    test_db, persistent_models, monkeypatch
):
    due_launching_job = _create_queued_job(
        status="launching", next_attempt_at=datetime.now(UTC) - timedelta(minutes=1)
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
    test_db, monkeypatch, mock_settings
):
    _patch_pbs_snapshot(
        monkeypatch,
        [
            _pbs_job("1", queue="workflow-exec", state="R"),
            _pbs_job("2", queue="workflow-exec", state="R"),
            _pbs_job("3", queue="workflow-exec", state="Q"),
            # Not counted: other states / other queues.
            _pbs_job("4", queue="workflow-exec", state="H"),
            _pbs_job("5", queue="workflow-exec", state="F"),
            _pbs_job("6", queue="normal", state="R"),
            _pbs_job("7", queue="gpuhopper", state="Q"),
        ],
        generated_at=datetime.now(UTC),
    )
    mock_settings.seqera.max_workflow_exec_jobs = 10

    assert scheduler_jobs.get_available_workflow_capacity(test_db, settings=mock_settings) == 7


def test_get_available_workflow_capacity_counts_jobs_submitted_after_snapshot(
    test_db, persistent_models, monkeypatch, mock_settings
):
    generated_at = datetime.now(UTC) - timedelta(minutes=10)
    before = _create_queued_job(status="submitted")
    before.submitted_at = generated_at - timedelta(minutes=1)
    after = _create_queued_job(status="submitted")
    after.submitted_at = generated_at + timedelta(minutes=1)
    test_db.add_all([before, after])
    test_db.commit()
    _patch_pbs_snapshot(
        monkeypatch, [_pbs_job("1", queue="workflow-exec", state="R")], generated_at
    )
    mock_settings.seqera.max_workflow_exec_jobs = 10

    # 1 in the snapshot + 1 submitted since it was generated.
    assert scheduler_jobs.get_available_workflow_capacity(test_db, settings=mock_settings) == 8


def test_get_available_workflow_capacity_floors_at_zero(test_db, monkeypatch, mock_settings):
    _patch_pbs_snapshot(
        monkeypatch,
        [_pbs_job(str(i), queue="workflow-exec", state="Q") for i in range(12)],
        generated_at=datetime.now(UTC),
    )
    mock_settings.seqera.max_workflow_exec_jobs = 10

    assert scheduler_jobs.get_available_workflow_capacity(test_db, settings=mock_settings) == 0


def test_submit_pending_jobs_skips_when_pbs_snapshot_unavailable(
    test_db, persistent_models, monkeypatch
):
    _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
    scheduler = _make_scheduler()

    async def _get_pbs_jobs(**_kwargs):
        raise GadiPbsJobsError("missing")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs.gadi_pbs_jobs, "get_pbs_jobs", _get_pbs_jobs)

    scheduler_jobs.submit_pending_jobs()

    assert scheduler.get_jobs(jobstore="memory") == []
