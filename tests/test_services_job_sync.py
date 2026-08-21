"""Tests for workflow run status/result sync service."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import job_sync
from app.services.seqera_errors import SeqeraAPIError
from tests.datagen import AppUserFactory, DataTransferFactory, WorkflowFactory, WorkflowRunFactory


def _create_run(
    *,
    seqera_run_id: str = "wf-sync-1",
    seqera_final_status: str | None = None,
    sync_completed_at: datetime | None = None,
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction", tool="boltz")
    return WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        seqera_run_id=seqera_run_id,
        seqera_final_status=seqera_final_status,
        sync_completed_at=sync_completed_at,
        tool="boltz",
    )


@pytest.mark.asyncio
async def test_sync_workflow_run_running_polls_without_finalizing_or_syncing(
    test_db, persistent_models, monkeypatch
):
    run = _create_run()
    describe = AsyncMock(return_value={"workflow": {"status": "RUNNING"}})
    sync_outputs = AsyncMock()
    ensure_score = AsyncMock()
    sync_usage = AsyncMock()

    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)
    monkeypatch.setattr(job_sync, "ensure_completed_run_score", ensure_score)
    monkeypatch.setattr(job_sync, "sync_service_usage", sync_usage)

    result = await job_sync.sync_workflow_run(test_db, run, describe_func=describe)

    test_db.refresh(run)
    describe.assert_awaited_once_with("wf-sync-1")
    sync_outputs.assert_not_awaited()
    ensure_score.assert_not_awaited()
    sync_usage.assert_not_awaited()
    assert result.terminal is False
    assert result.seqera_status == "RUNNING"
    assert run.seqera_final_status is None
    assert run.sync_completed_at is None


@pytest.mark.asyncio
async def test_sync_workflow_run_succeeded_creates_output_transfers_and_waits(
    test_db, persistent_models, monkeypatch
):
    run = _create_run()
    pending_transfer = DataTransferFactory.create_sync(
        workflow_run=run,
        direction="output",
        provider="globus",
        status="pending",
    )
    spec = MagicMock()
    spec.create_output_transfers.return_value = [pending_transfer]
    describe = AsyncMock(return_value={"workflow": {"status": "SUCCEEDED"}})
    sync_outputs = AsyncMock()
    ensure_score = AsyncMock(return_value=0.9)
    sync_usage = AsyncMock(return_value=1.25)

    monkeypatch.setattr(job_sync, "get_output_spec", lambda _run: spec)
    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)
    monkeypatch.setattr(job_sync, "ensure_completed_run_score", ensure_score)
    monkeypatch.setattr(job_sync, "sync_service_usage", sync_usage)

    result = await job_sync.sync_workflow_run(
        test_db,
        run,
        suppress_s3_errors=True,
        describe_func=describe,
    )

    test_db.refresh(run)
    describe.assert_awaited_once_with("wf-sync-1")
    spec.create_output_transfers.assert_called_once_with(test_db, run, settings=None)
    sync_outputs.assert_not_awaited()
    ensure_score.assert_not_awaited()
    sync_usage.assert_not_awaited()
    assert result.terminal is True
    assert result.sync_completed is False
    assert result.outputs_synced == 0
    assert run.seqera_final_status == "SUCCEEDED"
    assert run.sync_completed_at is None


@pytest.mark.asyncio
async def test_sync_workflow_run_succeeded_finalizes_after_output_transfers_complete(
    test_db, persistent_models, monkeypatch
):
    run = _create_run()
    completed_transfer = DataTransferFactory.create_sync(
        workflow_run=run,
        direction="output",
        provider="globus",
        status="completed",
    )
    spec = MagicMock()
    spec.create_output_transfers.return_value = [completed_transfer]
    describe = AsyncMock(return_value={"workflow": {"status": "SUCCEEDED"}})
    sync_outputs = AsyncMock(return_value=["results/report.html", "results/scores.csv"])
    ensure_score = AsyncMock(return_value=0.9)
    sync_usage = AsyncMock(return_value=1.25)

    monkeypatch.setattr(job_sync, "get_output_spec", lambda _run: spec)
    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)
    monkeypatch.setattr(job_sync, "ensure_completed_run_score", ensure_score)
    monkeypatch.setattr(job_sync, "sync_service_usage", sync_usage)

    result = await job_sync.sync_workflow_run(
        test_db,
        run,
        suppress_s3_errors=True,
        describe_func=describe,
    )

    test_db.refresh(run)
    describe.assert_awaited_once_with("wf-sync-1")
    spec.create_output_transfers.assert_called_once_with(test_db, run, settings=None)
    sync_outputs.assert_awaited_once_with(
        test_db,
        run=run,
        spec=spec,
        suppress_s3_errors=True,
    )
    ensure_score.assert_awaited_once_with(test_db, run, "Completed")
    sync_usage.assert_awaited_once_with(test_db, run, "Completed")
    assert result.terminal is True
    assert result.sync_completed is True
    assert result.outputs_synced == 2
    assert run.seqera_final_status == "SUCCEEDED"
    assert run.sync_completed_at is not None


@pytest.mark.asyncio
async def test_sync_workflow_run_succeeded_reports_failed_output_transfer(
    test_db, persistent_models, monkeypatch
):
    run = _create_run()
    failed_transfer = DataTransferFactory.create_sync(
        workflow_run=run,
        direction="output",
        provider="globus",
        status="failed",
        error_message="no such file",
    )
    spec = MagicMock()
    spec.create_output_transfers.return_value = [failed_transfer]
    describe = AsyncMock(return_value={"workflow": {"status": "SUCCEEDED"}})
    sync_outputs = AsyncMock()

    monkeypatch.setattr(job_sync, "get_output_spec", lambda _run: spec)
    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)

    result = await job_sync.sync_workflow_run(test_db, run, describe_func=describe)

    test_db.refresh(run)
    sync_outputs.assert_not_awaited()
    assert result.terminal is True
    assert result.sync_completed is False
    assert "Output transfer failed" in result.error
    assert "no such file" in result.error
    assert run.seqera_final_status == "SUCCEEDED"
    assert run.sync_completed_at is None


@pytest.mark.asyncio
async def test_sync_workflow_run_sync_completed_reflects_finalized_state(
    test_db, persistent_models, monkeypatch
):
    run = _create_run(seqera_final_status="SUCCEEDED")
    describe = AsyncMock()
    finalize = AsyncMock(return_value=0)

    monkeypatch.setattr(
        job_sync,
        "_ensure_completed_run_output_transfers",
        lambda _db, _run, settings=None: job_sync.OutputTransferState(ready=True),
    )
    monkeypatch.setattr(job_sync, "finalize_completed_workflow_run", finalize)

    result = await job_sync.sync_workflow_run(test_db, run, describe_func=describe)

    test_db.refresh(run)
    describe.assert_not_awaited()
    finalize.assert_awaited_once_with(
        test_db,
        run,
        force=False,
        suppress_s3_errors=False,
        settings=None,
    )
    assert result.terminal is True
    assert result.sync_completed is False
    assert run.sync_completed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seqera_status", ["FAILED", "CANCELLED"])
async def test_sync_workflow_run_terminal_non_success_records_status_without_result_sync(
    test_db, persistent_models, monkeypatch, seqera_status
):
    run = _create_run()
    describe = AsyncMock(return_value={"workflow": {"status": seqera_status}})
    sync_outputs = AsyncMock()
    ensure_score = AsyncMock()
    sync_usage = AsyncMock()

    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)
    monkeypatch.setattr(job_sync, "ensure_completed_run_score", ensure_score)
    monkeypatch.setattr(job_sync, "sync_service_usage", sync_usage)

    result = await job_sync.sync_workflow_run(test_db, run, describe_func=describe)

    test_db.refresh(run)
    describe.assert_awaited_once_with("wf-sync-1")
    sync_outputs.assert_not_awaited()
    ensure_score.assert_not_awaited()
    sync_usage.assert_not_awaited()
    assert result.terminal is True
    assert result.sync_completed is False
    assert result.seqera_status == seqera_status
    assert run.seqera_final_status == seqera_status
    assert run.sync_completed_at is None


def test_get_runs_requiring_sync_excludes_non_success_terminal_runs(test_db, persistent_models):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction", tool="boltz")
    failed_run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        seqera_run_id="wf-failed",
        seqera_final_status="FAILED",
        sync_completed_at=None,
    )
    cancelled_run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        seqera_run_id="wf-cancelled",
        seqera_final_status="CANCELLED",
        sync_completed_at=None,
    )
    successful_unsynced_run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        seqera_run_id="wf-success",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
    )

    runs = job_sync.get_runs_requiring_sync(test_db)

    assert successful_unsynced_run in runs
    assert failed_run not in runs
    assert cancelled_run not in runs


@pytest.mark.asyncio
async def test_sync_workflow_run_already_fully_synced_skips_seqera_and_result_sync(
    test_db, persistent_models, monkeypatch
):
    completed_at = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
    run = _create_run(seqera_final_status="SUCCEEDED", sync_completed_at=completed_at)
    describe = AsyncMock()
    sync_outputs = AsyncMock()
    ensure_score = AsyncMock()
    sync_usage = AsyncMock()

    monkeypatch.setattr(job_sync, "sync_workflow_outputs", sync_outputs)
    monkeypatch.setattr(job_sync, "ensure_completed_run_score", ensure_score)
    monkeypatch.setattr(job_sync, "sync_service_usage", sync_usage)

    result = await job_sync.sync_workflow_run(test_db, run, describe_func=describe)

    test_db.refresh(run)
    describe.assert_not_awaited()
    sync_outputs.assert_not_awaited()
    ensure_score.assert_not_awaited()
    sync_usage.assert_not_awaited()
    assert result.skipped is True
    assert result.sync_completed is True
    assert run.sync_completed_at == completed_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_sync_workflow_runs_records_seqera_error_without_marking_complete(
    test_db, persistent_models
):
    run = _create_run()
    describe = AsyncMock(side_effect=SeqeraAPIError("Seqera unavailable", status_code=500))

    result = await job_sync.sync_workflow_runs(test_db, describe_func=describe)

    test_db.refresh(run)
    describe.assert_awaited_once_with("wf-sync-1")
    assert result.checked == 1
    assert result.errored == 1
    assert result.results[0].error == "Seqera unavailable"
    assert run.seqera_final_status is None
    assert run.sync_completed_at is None
