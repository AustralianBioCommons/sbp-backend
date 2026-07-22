from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.scheduler import jobs as scheduler_jobs
from app.services.seqera import WorkflowLaunchResult
from tests.datagen import AppUserFactory, QueuedJobFactory, WorkflowFactory, WorkflowRunFactory


def _get_db_override(db):
    def _get_db():
        yield db

    return _get_db


async def _failing_launch(**_kwargs):
    raise RuntimeError("Seqera launch failed")


def _create_queued_job(
    *, attempts: int = 0, workflow_name: str = "de-novo-design", tool: str | None = None
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name=workflow_name)
    workflow_run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        work_dir=f"/work/{workflow_name}-{attempts}-{uuid4()}",
        seqera_run_id=None,
        tool=tool,
    )
    return QueuedJobFactory.create_sync(
        workflow_run=workflow_run,
        workflow=workflow,
        launch_payload={},
        status="pending",
        attempts=attempts,
        error=None,
        submitted_at=None,
        last_attempt_at=None,
        next_attempt_at=datetime.now(UTC),
    )


def test_is_seqera_available_returns_health_status(test_db, monkeypatch):
    async def _healthy_status(_db):
        return SimpleNamespace(overall_status="healthy")

    async def _unhealthy_status(_db):
        return SimpleNamespace(overall_status="degraded")

    monkeypatch.setattr(scheduler_jobs.health, "get_system_status", _healthy_status)
    assert scheduler_jobs.is_seqera_available(test_db) is True

    monkeypatch.setattr(scheduler_jobs.health, "get_system_status", _unhealthy_status)
    assert scheduler_jobs.is_seqera_available(test_db) is False


def test_launch_job_skips_when_seqera_unavailable(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: False)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert queued_job.attempts == 0
    assert queued_job.status == "pending"
    assert queued_job.last_attempt_at is None


def test_launch_job_ignores_missing_job(test_db, monkeypatch):
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)

    scheduler_jobs.launch_job(uuid4())


def test_launch_job_submits_successful_bindflow_job(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return WorkflowLaunchResult(workflow_id="seqera-run-123", status="submitted")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": False}]
    assert queued_job.workflow_run.seqera_run_id == "seqera-run-123"
    assert queued_job.attempts == 1
    assert queued_job.status == "submitted"
    assert queued_job.submitted_at is not None
    assert queued_job.last_attempt_at == queued_job.submitted_at
    assert queued_job.next_attempt_at is None
    assert queued_job.error is None


def test_launch_job_dry_run_does_not_update_job(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return WorkflowLaunchResult(workflow_id="seqera-run-123", status="submitted")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id, dry_run=True)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": True}]
    assert queued_job.workflow_run.seqera_run_id is None
    assert queued_job.attempts == 0
    assert queued_job.status == "pending"
    assert queued_job.submitted_at is None
    assert queued_job.last_attempt_at is None
    assert queued_job.next_attempt_at is not None


def test_launch_job_dispatches_proteindj_for_rfdiffusion_tool(
    test_db, persistent_models, monkeypatch
):
    queued_job = _create_queued_job(tool="rfdiffusion")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return WorkflowLaunchResult(workflow_id="seqera-run-rfd", status="submitted")

    async def _unexpected_bindflow_launch(**_kwargs):
        raise AssertionError("launch_bindflow_workflow should not be called for rfdiffusion")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_proteindj_workflow", _successful_launch)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _unexpected_bindflow_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": False}]
    assert queued_job.workflow_run.seqera_run_id == "seqera-run-rfd"
    assert queued_job.status == "submitted"


def test_launch_job_dispatches_bindflow_for_bindcraft_tool(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(tool="bindcraft")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return WorkflowLaunchResult(workflow_id="seqera-run-bc", status="submitted")

    async def _unexpected_proteindj_launch(**_kwargs):
        raise AssertionError("launch_proteindj_workflow should not be called for bindcraft")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)
    monkeypatch.setattr(scheduler_jobs, "launch_proteindj_workflow", _unexpected_proteindj_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": False}]
    assert queued_job.workflow_run.seqera_run_id == "seqera-run-bc"
    assert queued_job.status == "submitted"


def test_launch_job_dispatches_wisps_workflows(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="interaction-screening")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_wisps_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": False}]
    assert queued_job.workflow_run.seqera_run_id is None
    assert queued_job.status == "submitted"


def test_launch_job_dispatches_proteinfold_workflows(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="single-prediction")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_proteinfold_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert calls == [{"queued_job": queued_job, "dry_run": False}]
    assert queued_job.workflow_run.seqera_run_id is None
    assert queued_job.status == "submitted"


def test_launch_job_rejects_unsupported_workflow(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="unsupported-workflow")
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)

    with pytest.raises(ValueError, match="Unsupported workflow"):
        scheduler_jobs.launch_job(queued_job.id)


def test_launch_job_dry_run_failure_does_not_update_job(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _failing_launch)

    scheduler_jobs.launch_job(queued_job.id, dry_run=True)

    test_db.refresh(queued_job)
    assert queued_job.attempts == 0
    assert queued_job.status == "pending"
    assert queued_job.error is None
    assert queued_job.last_attempt_at is None
    assert queued_job.next_attempt_at is not None


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
