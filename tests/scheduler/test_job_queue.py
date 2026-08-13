from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.scheduler import jobs as scheduler_jobs
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


def test_submit_pending_jobs_skips_when_seqera_unavailable(test_db, persistent_models, monkeypatch, mock_settings):
    due_job = _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
    scheduler = _make_scheduler()
    checked_sessions = []

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(
        scheduler_jobs,
        "is_seqera_available",
        lambda db_session: checked_sessions.append(db_session) or False,
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session: True)
    monkeypatch.setattr(
        scheduler_jobs,
        "get_available_workflow_capacity",
        lambda **_kwargs: 25,
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session: True)
    monkeypatch.setattr(
        scheduler_jobs,
        "get_available_workflow_capacity",
        lambda **_kwargs: 25,
    )

    scheduler_jobs.submit_pending_jobs()

    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert [job.id for job in scheduled_jobs] == [launch_id]
    assert scheduled_jobs[0].kwargs == {"job_id": due_job.id, "dry_run": False}


def test_submit_pending_jobs_skips_when_no_gadi_capacity(test_db, persistent_models, monkeypatch):
    _create_queued_job(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
    scheduler = _make_scheduler()

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "SCHEDULER", scheduler)
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session: True)
    monkeypatch.setattr(scheduler_jobs, "get_available_workflow_capacity", lambda **_kwargs: 0)

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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db_session: True)
    monkeypatch.setattr(scheduler_jobs, "get_available_workflow_capacity", lambda **_kwargs: 2)

    scheduler_jobs.submit_pending_jobs()

    scheduled_jobs = scheduler.get_jobs(jobstore="memory")
    assert len(scheduled_jobs) == 2
    scheduled_job_ids = {job.kwargs["job_id"] for job in scheduled_jobs}
    assert scheduled_job_ids.issubset({job.id for job in due_jobs})


def test_get_available_workflow_capacity_uses_seqera_active_count(monkeypatch, mock_settings):
    async def _count_active_workflows(**_kwargs):
        return 20

    monkeypatch.setattr(scheduler_jobs.seqera, "count_active_workflows", _count_active_workflows)
    mock_settings.seqera.max_concurrent_workflows = 25

    assert scheduler_jobs.get_available_workflow_capacity(settings=mock_settings) == 5


def test_get_available_workflow_capacity_floors_at_zero(monkeypatch, mock_settings):
    async def _count_active_workflows(**_kwargs):
        return 30

    monkeypatch.setattr(scheduler_jobs.seqera, "count_active_workflows", _count_active_workflows)
    mock_settings.seqera.max_concurrent_workflows = 25

    assert scheduler_jobs.get_available_workflow_capacity(settings=mock_settings) == 0
