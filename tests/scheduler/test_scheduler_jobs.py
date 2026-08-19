from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pytest_mock import MockerFixture

from app.db.models.core import AppUser
from app.routes.dependencies import get_current_user_id
from app.scheduler import jobs as scheduler_jobs
from app.services.seqera import WorkflowLaunchResult
from tests.datagen import (
    AppUserFactory,
    DataTransferFactory,
    QueuedJobFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


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


def test_with_scheduler_db_session_closes_db_dependency(monkeypatch):
    session = object()
    closed = []
    seen_sessions = []

    def _get_db():
        try:
            yield session
        finally:
            closed.append(True)

    @scheduler_jobs.with_scheduler_db_session
    def _job(*, db_session):
        seen_sessions.append(db_session)
        return "done"

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db)

    assert _job() == "done"
    assert seen_sessions == [session]
    assert closed == [True]


def test_with_scheduler_db_session_closes_db_dependency_on_error(monkeypatch):
    session = object()
    closed = []

    def _get_db():
        try:
            yield session
        finally:
            closed.append(True)

    @scheduler_jobs.with_scheduler_db_session
    def _job(*, db_session):
        assert db_session is session
        raise RuntimeError("job failed")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db)

    with pytest.raises(RuntimeError, match="job failed"):
        _job()

    assert closed == [True]


def test_is_seqera_available_returns_health_status(test_db, monkeypatch):
    async def _healthy_status(_db, **_kwargs):
        return SimpleNamespace(overall_status="healthy")

    async def _unhealthy_status(_db, **_kwargs):
        return SimpleNamespace(overall_status="degraded")

    monkeypatch.setattr(scheduler_jobs.health, "get_system_status", _healthy_status)
    assert scheduler_jobs.is_seqera_available(test_db) is True

    monkeypatch.setattr(scheduler_jobs.health, "get_system_status", _unhealthy_status)
    assert scheduler_jobs.is_seqera_available(test_db) is False


def test_launch_job_skips_when_seqera_unavailable(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: False)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert queued_job.attempts == 0
    assert queued_job.status == "pending"
    assert queued_job.last_attempt_at is None


def test_launch_job_ignores_missing_job(test_db, monkeypatch):
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)

    scheduler_jobs.launch_job(uuid4())


def test_launch_job_submits_successful_bindflow_job(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return WorkflowLaunchResult(workflow_id="seqera-run-123", status="submitted")

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is False
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id, dry_run=True)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is True
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_proteindj_workflow", _successful_launch)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _unexpected_bindflow_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is False
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _successful_launch)
    monkeypatch.setattr(scheduler_jobs, "launch_proteindj_workflow", _unexpected_proteindj_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is False
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
    assert queued_job.workflow_run.seqera_run_id == "seqera-run-bc"
    assert queued_job.status == "submitted"


def test_launch_job_dispatches_wisps_workflows(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="interaction-screening")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_wisps_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is False
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
    assert queued_job.workflow_run.seqera_run_id is None
    assert queued_job.status == "submitted"


def test_launch_job_dispatches_proteinfold_workflows(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="single-prediction")
    calls = []

    async def _successful_launch(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_proteinfold_workflow", _successful_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert len(calls) == 1
    assert calls[0]["queued_job"] is queued_job
    assert calls[0]["dry_run"] is False
    assert calls[0]["settings"] is scheduler_jobs.get_settings()
    assert queued_job.workflow_run.seqera_run_id is None
    assert queued_job.status == "submitted"


def test_launch_job_rejects_unsupported_workflow(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job(workflow_name="unsupported-workflow")
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)

    with pytest.raises(ValueError, match="Unsupported workflow"):
        scheduler_jobs.launch_job(queued_job.id)


def test_launch_job_dry_run_failure_does_not_update_job(test_db, persistent_models, monkeypatch):
    queued_job = _create_queued_job()
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
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
    monkeypatch.setattr(scheduler_jobs, "is_seqera_available", lambda _db, **_kwargs: True)
    monkeypatch.setattr(scheduler_jobs, "launch_bindflow_workflow", _failing_launch)

    scheduler_jobs.launch_job(queued_job.id)

    test_db.refresh(queued_job)
    assert queued_job.attempts == scheduler_jobs.LAUNCH_MAX_ATTEMPTS
    assert queued_job.status == "failed"
    assert queued_job.error == "Seqera launch failed"
    assert queued_job.last_attempt_at is not None
    assert queued_job.next_attempt_at is None


def test_refresh_user_credits_dry_run_does_not_modify_db(test_db, monkeypatch):
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    user = AppUser(
        auth0_user_id="auth0|dry-run",
        name="Dry Run",
        email="dry-run@example.com",
        credit=50,
        sbp_bundle_credit_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    test_db.add(user)
    test_db.commit()

    scheduler_jobs.refresh_user_credits(dry_run=True)

    test_db.refresh(user)
    assert user.credit == 50


def test_refresh_user_credits_only_resets_approved_users(test_db, monkeypatch):
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    unapproved_user = AppUser(
        auth0_user_id="auth0|unapproved",
        name="Unapproved",
        email="unapproved@example.com",
        credit=10,
    )
    already_granted_at = datetime(2026, 1, 1, tzinfo=UTC)
    approved_user = AppUser(
        auth0_user_id="auth0|approved",
        name="Approved",
        email="approved@example.com",
        credit=200,
        sbp_bundle_credit_granted_at=already_granted_at,
    )
    test_db.add_all([unapproved_user, approved_user])
    test_db.commit()

    scheduler_jobs.refresh_user_credits()

    test_db.refresh(unapproved_user)
    test_db.refresh(approved_user)
    # Never went through role approval, so the monthly refresh leaves them untouched.
    assert unapproved_user.credit == 10
    assert unapproved_user.sbp_bundle_credit_granted_at is None
    assert approved_user.credit == scheduler_jobs.SBP_USER_CREDIT_ALLOWANCE
    assert approved_user.credit_updated_by == scheduler_jobs.MONTHLY_CREDIT_REFRESH_ACTOR
    # (SQLite round-trips DateTime(timezone=True) values as naive, hence replace().)
    assert approved_user.sbp_bundle_credit_granted_at.replace(tzinfo=UTC) == already_granted_at


def test_refresh_user_credits_does_not_double_grant_across_refresh_cycles(
    test_db, monkeypatch, mocker: MockerFixture, mock_settings
):
    """Once a user is approved and refreshed, a later refresh cycle must not
    stack another SBP_USER_CREDIT_ALLOWANCE on top of their current balance."""
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    user = AppUser(auth0_user_id="auth0|race", name="Race", email="race@example.com", credit=0)
    test_db.add(user)
    test_db.commit()

    # Unapproved: a refresh before role approval is a no-op.
    scheduler_jobs.refresh_user_credits()
    test_db.refresh(user)
    assert user.credit == 0

    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={
            "sub": "auth0|race",
            "https://biocommons.org.au/roles": ["biocommons/group/sbp_workflow_execution"],
        },
    )
    mocker.patch("app.routes.dependencies.fetch_userinfo_claims", return_value={})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")

    get_current_user_id(credentials, test_db, mock_settings)
    test_db.refresh(user)
    assert user.credit == scheduler_jobs.SBP_USER_CREDIT_ALLOWANCE

    # Now approved: further refresh cycles reset, not add.
    scheduler_jobs.refresh_user_credits()
    test_db.refresh(user)
    assert user.credit == scheduler_jobs.SBP_USER_CREDIT_ALLOWANCE


def test_sync_data_transfers_dry_run_does_not_call_globus_transfer(
    test_db, persistent_models, monkeypatch, mocker: MockerFixture
):
    """dry_run only counts eligible rows - it must never touch Globus or the DB."""
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    workflow_run = WorkflowRunFactory.create_sync()
    DataTransferFactory.create_sync(workflow_run=workflow_run, provider="globus", status="pending")
    DataTransferFactory.create_sync(
        workflow_run=workflow_run, provider="globus", status="completed"
    )
    DataTransferFactory.create_sync(workflow_run=workflow_run, provider="s3", status="pending")
    mock_sync = mocker.patch.object(scheduler_jobs.globus_transfer, "sync_data_transfers")

    scheduler_jobs.sync_data_transfers(dry_run=True)

    mock_sync.assert_not_called()


def test_sync_data_transfers_calls_globus_transfer_sync(
    test_db, monkeypatch, mocker: MockerFixture
):
    monkeypatch.setattr(scheduler_jobs, "get_db", _get_db_override(test_db))
    mock_result = SimpleNamespace(
        checked=2,
        submitted=1,
        completed=1,
        failed=0,
        errored=0,
        finalized_runs=0,
    )
    mock_sync = mocker.patch.object(
        scheduler_jobs.globus_transfer, "sync_data_transfers", return_value=mock_result
    )

    scheduler_jobs.sync_data_transfers(dry_run=False)

    mock_sync.assert_called_once_with(test_db, limit=scheduler_jobs.DATA_TRANSFER_SYNC_BATCH_LIMIT)
