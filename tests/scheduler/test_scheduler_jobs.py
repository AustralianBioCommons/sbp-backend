from datetime import UTC, datetime, timedelta

from app.scheduler import jobs as scheduler_jobs
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory


def _get_db_override(db):
    def _get_db():
        yield db

    return _get_db


async def _failing_launch(**_kwargs):
    raise RuntimeError("Seqera launch failed")


def _create_queued_job(*, attempts: int = 0):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="de-novo-design")
    workflow_run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        work_dir=f"/work/{attempts}",
    )
    return QueuedJobFactory.create_sync(
        workflow_run=workflow_run,
        workflow=workflow,
        launch_payload={},
        status="pending",
        attempts=attempts,
        next_attempt_at=datetime.now(UTC),
    )


def test_launch_job_counts_failed_attempt_and_schedules_retry(
    test_db, persistent_models, monkeypatch
):
    queued_job = _create_queued_job()
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _failing_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert queued_job.attempts == 1
    assert queued_job.status == "pending"
    assert queued_job.error == "Seqera launch failed"
    assert queued_job.last_attempt_at is not None
    assert queued_job.next_attempt_at is not None
    assert queued_job.next_attempt_at - queued_job.last_attempt_at == timedelta(
        seconds=scheduler_jobs.RETRY_DELAY_BASE
    )


def test_launch_job_marks_failed_after_max_failed_attempts(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(attempts=scheduler_jobs.LAUNCH_MAX_ATTEMPTS - 1)
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _failing_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert queued_job.attempts == scheduler_jobs.LAUNCH_MAX_ATTEMPTS
    assert queued_job.status == "failed"
    assert queued_job.error == "Seqera launch failed"
    assert queued_job.last_attempt_at is not None
    assert queued_job.next_attempt_at is None
