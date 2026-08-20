"""Tests for Globus data-transfer submission, polling, and launch-gate notification."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.config import GlobusSettings
from app.services.globus_errors import GlobusTransferError
from app.services.globus_transfer import (
    STALE_TRANSFER_TIMEOUT,
    _gadi_relative_path,
    _notify_launcher,
    _s3_relative_path,
    build_gadi_input_path,
    build_gadi_output_path,
    poll_transfer,
    reset_failed_output_transfers,
    submit_pending_transfer,
    sync_data_transfers,
)
from tests.datagen import DataTransferFactory, QueuedJobFactory, WorkflowRunFactory


def _globus_api_error(status_code: int, json_body: dict) -> Exception:
    """Build a real globus_sdk.TransferAPIError - the constructor needs a full
    requests.Response, so this is easier to reuse than hand-rolling one per test.
    TransferAPIError is a concrete globus_sdk.GlobusAPIError subclass, which is
    the type the production code actually catches."""
    import globus_sdk

    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(json_body).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.request = requests.PreparedRequest()
    response.request.prepare(method="GET", url="https://transfer.api.globus.org/v0.10/x")
    return globus_sdk.TransferAPIError(response)


@pytest.fixture
def mock_transfer_client():
    client = MagicMock()
    with patch("app.services.globus_transfer.get_transfer_client", return_value=client):
        yield client


@pytest.fixture
def globus_settings():
    return GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/test",
        input_dir="/test/input",
        output_dir="/test/output",
    )


# ============================================================================
# Path helpers
# ============================================================================


def test_build_gadi_input_path():
    globus_settings = GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/g/data/yz52/sbp_data",
        input_dir="/g/data/yz52/sbp_data/dev_input",
        output_dir="/g/data/yz52/sbp_data/dev_output",
    )
    path = build_gadi_input_path(
        "run-123",
        "single-prediction",
        "sample.csv",
        globus_settings=globus_settings,
    )
    assert path == "/g/data/yz52/sbp_data/dev_input/single-prediction/run-123/sample.csv"


def test_build_gadi_output_path():
    globus_settings = GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/g/data/yz52/sbp_data",
        input_dir="/g/data/yz52/sbp_data/dev_input",
        output_dir="/g/data/yz52/sbp_data/dev_output",
    )

    assert (
        build_gadi_output_path(
            "run-123",
            "single-prediction",
            globus_settings=globus_settings,
        )
        == "/g/data/yz52/sbp_data/dev_output/single-prediction/run-123"
    )
    assert (
        build_gadi_output_path(
            "run-123",
            "single-prediction",
            "reports/",
            globus_settings=globus_settings,
        )
        == "/g/data/yz52/sbp_data/dev_output/single-prediction/run-123/reports/"
    )


def test_s3_relative_path_strips_bucket():
    assert _s3_relative_path("s3://my-bucket/inputs/samplesheets/a.csv") == (
        "/inputs/samplesheets/a.csv"
    )


def test_s3_relative_path_rejects_non_s3_uri():
    with pytest.raises(GlobusTransferError, match="Not an S3 URI"):
        _s3_relative_path("/local/path.csv")


def test_s3_relative_path_rejects_missing_key():
    with pytest.raises(GlobusTransferError, match="missing a bucket or object key"):
        _s3_relative_path("s3://my-bucket")


def test_gadi_relative_path_strips_collection_root():
    globus_settings = GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/g/data/yz52/sbp_data",
        input_dir="/g/data/yz52/sbp_data/dev_input",
        output_dir="/g/data/yz52/sbp_data/dev_output",
    )
    result = _gadi_relative_path(
        "/g/data/yz52/sbp_data/dev_input/run-1/a.csv",
        globus_settings=globus_settings,
    )
    assert result == "/dev_input/run-1/a.csv"


def test_gadi_relative_path_rejects_path_outside_root():
    globus_settings = GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/g/data/yz52/sbp_data",
        input_dir="/g/data/yz52/sbp_data/dev_input",
        output_dir="/g/data/yz52/sbp_data/dev_output",
    )
    with pytest.raises(GlobusTransferError, match="not under the Gadi collection root"):
        _gadi_relative_path("/some/other/path/a.csv", globus_settings=globus_settings)


# ============================================================================
# submit_pending_transfer
# ============================================================================


def test_submit_pending_transfer_success(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-123"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-abc"}

    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="pending",
        source_location="s3://my-bucket/inputs/samplesheets/a.csv",
        destination_location="/test/input/single-prediction/run-1/a.csv",
        transfer_id=None,
    )

    submit_pending_transfer(test_db, data_transfer)

    assert data_transfer.status == "in_progress"
    # transfer_id held the submission id mid-flight, but is overwritten with the
    # real Globus task id once submission succeeds.
    assert data_transfer.transfer_id == "task-abc"

    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["source_endpoint"] == "test-s3-collection-id"
    assert submitted["destination_endpoint"] == "test-gadi-collection-id"
    assert submitted["submission_id"] == "sub-123"
    assert submitted["DATA"][0]["source_path"] == "/inputs/samplesheets/a.csv"
    assert submitted["DATA"][0]["destination_path"] == "/input/single-prediction/run-1/a.csv"


def test_submit_pending_transfer_output_success(
    test_db, persistent_models, mock_transfer_client, globus_settings
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-123"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-abc"}

    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="globus",
        status="pending",
        source_location="/test/output/single-prediction/run-1/reports/",
        destination_location="s3://my-bucket/results/run-1/reports/",
        recursive=True,
        transfer_id=None,
    )

    submit_pending_transfer(test_db, data_transfer, globus_settings=globus_settings)

    assert data_transfer.status == "in_progress"
    assert data_transfer.transfer_id == "task-abc"

    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["source_endpoint"] == "test-gadi-collection-id"
    assert submitted["destination_endpoint"] == "test-s3-collection-id"
    assert submitted["submission_id"] == "sub-123"
    assert submitted["DATA"][0]["source_path"] == "/output/single-prediction/run-1/reports/"
    assert submitted["DATA"][0]["destination_path"] == "/results/run-1/reports/"
    assert submitted["DATA"][0]["recursive"] is True


def test_submit_pending_transfer_uses_recursive_column(
    test_db, persistent_models, mock_transfer_client, globus_settings
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-123"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-abc"}

    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="globus",
        status="pending",
        source_location="/test/output/single-prediction/run-1/reports/",
        destination_location="s3://my-bucket/results/run-1/reports/",
        recursive=False,
        transfer_id=None,
    )

    submit_pending_transfer(test_db, data_transfer, globus_settings=globus_settings)

    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["DATA"][0]["recursive"] is False


def test_submit_pending_transfer_reuses_existing_submission_id(
    test_db, persistent_models, mock_transfer_client
):
    """A crash between persisting submission_id and the final status commit must
    not mint a fresh submission_id on retry - Globus dedupes on this id."""
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-abc"}

    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="pending",
        source_location="s3://my-bucket/inputs/samplesheets/a.csv",
        destination_location="/test/input/single-prediction/run-1/a.csv",
        # Simulates a crash after the submission id was committed to transfer_id
        # but before submit_transfer completed - it must be reused, not replaced.
        transfer_id="already-committed-sub-id",
    )

    submit_pending_transfer(test_db, data_transfer)

    mock_transfer_client.get_submission_id.assert_not_called()
    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["submission_id"] == "already-committed-sub-id"


def test_submit_pending_transfer_api_error_marks_failed(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-123"}
    mock_transfer_client.submit_transfer.side_effect = _globus_api_error(
        400, {"code": "UNKNOWN_SCOPE_ERROR", "message": "requested unknown scopes"}
    )

    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="pending",
        source_location="s3://my-bucket/inputs/samplesheets/a.csv",
        destination_location="/test/input/single-prediction/run-1/a.csv",
        transfer_id=None,
    )

    submit_pending_transfer(test_db, data_transfer)

    assert data_transfer.status == "failed"
    assert "UNKNOWN_SCOPE_ERROR" in data_transfer.error_message
    assert data_transfer.transfer_id is None


def test_reset_failed_output_transfers_clears_retry_state(test_db, persistent_models):
    workflow_run = WorkflowRunFactory.create_sync()
    failed_output = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="globus",
        status="failed",
        transfer_id="task-stale",
        error_message="no such file",
    )
    failed_input = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="failed",
        transfer_id="task-input",
        error_message="input failed",
    )
    failed_s3_output = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="s3",
        status="failed",
        transfer_id="s3-transfer",
        error_message="s3 failed",
    )

    reset_count = reset_failed_output_transfers(
        test_db,
        transfer_ids=[failed_output.id, failed_input.id, failed_s3_output.id],
    )

    assert reset_count == 1
    assert failed_output.status == "pending"
    assert failed_output.transfer_id is None
    assert failed_output.error_message is None
    assert failed_output.updated_at is not None

    assert failed_input.status == "failed"
    assert failed_input.transfer_id == "task-input"
    assert failed_input.error_message == "input failed"
    assert failed_s3_output.status == "failed"
    assert failed_s3_output.transfer_id == "s3-transfer"
    assert failed_s3_output.error_message == "s3 failed"


# ============================================================================
# poll_transfer
# ============================================================================


def test_poll_transfer_no_transfer_id_raises(test_db, persistent_models):
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, status="in_progress", transfer_id=None
    )
    with pytest.raises(GlobusTransferError, match="no transfer_id"):
        poll_transfer(test_db, data_transfer)


def test_poll_transfer_api_error_records_message_without_changing_status(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_task.side_effect = _globus_api_error(
        401, {"code": "AuthenticationFailed", "message": "token expired"}
    )
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, status="in_progress", transfer_id="task-abc"
    )

    with pytest.raises(GlobusTransferError, match="Failed to poll"):
        poll_transfer(test_db, data_transfer)

    # Status is left untouched - the underlying Globus transfer may still be fine,
    # we just couldn't observe it - but the error is recorded for visibility.
    assert data_transfer.status == "in_progress"
    assert "Poll failed" in data_transfer.error_message


def test_poll_transfer_succeeded_marks_completed(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, status="in_progress", transfer_id="task-abc"
    )

    poll_transfer(test_db, data_transfer)

    assert data_transfer.status == "completed"


def test_poll_transfer_failed_records_fatal_error(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_task.return_value = {
        "status": "FAILED",
        "fatal_error": {"description": "no such file"},
    }
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, status="in_progress", transfer_id="task-abc"
    )

    poll_transfer(test_db, data_transfer)

    assert data_transfer.status == "failed"
    assert data_transfer.error_message == "no such file"


def test_poll_transfer_active_stays_in_progress(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_task.return_value = {"status": "ACTIVE"}
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, status="in_progress", transfer_id="task-abc"
    )

    poll_transfer(test_db, data_transfer)

    assert data_transfer.status == "in_progress"


def test_poll_transfer_inactive_recent_stays_in_progress(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_task.return_value = {"status": "INACTIVE"}
    workflow_run = WorkflowRunFactory.create_sync()
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        status="in_progress",
        transfer_id="task-abc",
        created_at=datetime.now(UTC),
        error_message=None,
    )

    poll_transfer(test_db, data_transfer)

    assert data_transfer.status == "in_progress"
    assert data_transfer.error_message is None


def test_poll_transfer_inactive_stale_marks_failed(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_task.return_value = {"status": "INACTIVE"}
    workflow_run = WorkflowRunFactory.create_sync()
    stale_created_at = datetime.now(UTC) - STALE_TRANSFER_TIMEOUT - timedelta(minutes=1)
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        status="in_progress",
        transfer_id="task-abc",
        created_at=stale_created_at,
    )

    poll_transfer(test_db, data_transfer)

    assert data_transfer.status == "failed"
    assert "INACTIVE" in data_transfer.error_message


# ============================================================================
# _notify_launcher
# ============================================================================


def test_notify_launcher_ignores_output_direction(test_db, persistent_models):
    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="output", status="completed"
    )

    _notify_launcher(test_db, data_transfer)

    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "staging"


def test_notify_launcher_ignores_non_staging_queued_job(test_db, persistent_models):
    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="pending")
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="input", status="completed"
    )

    _notify_launcher(test_db, data_transfer)

    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "pending"


def test_notify_launcher_failed_transfer_fails_queued_job(test_db, persistent_models):
    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    data_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        status="failed",
        error_message="disk full",
    )

    _notify_launcher(test_db, data_transfer)

    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "failed"
    assert "disk full" in queued_job.error


def test_notify_launcher_waits_for_all_input_transfers(test_db, persistent_models):
    """A run with two input transfers (samplesheet + pdb) must not launch until
    both have completed."""
    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    completed_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="input", status="completed"
    )
    DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="input", status="in_progress"
    )

    _notify_launcher(test_db, completed_transfer)

    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "staging"


def test_notify_launcher_flips_to_pending_once_all_input_transfers_complete(
    test_db, persistent_models
):
    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    first_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="input", status="completed"
    )
    second_transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run, direction="input", status="completed"
    )

    _notify_launcher(test_db, first_transfer)
    _notify_launcher(test_db, second_transfer)

    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "pending"


# ============================================================================
# sync_data_transfers
# ============================================================================


def test_sync_data_transfers_submits_and_notifies(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}

    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="pending",
        source_location="s3://my-bucket/inputs/samplesheets/a.csv",
        destination_location="/test/input/single-prediction/run-1/a.csv",
        transfer_id=None,
    )

    result = sync_data_transfers(test_db)

    assert result.checked == 1
    assert result.submitted == 1
    assert result.completed == 0
    assert result.failed == 0
    assert result.errored == 0


def test_sync_data_transfers_polls_and_completes(test_db, persistent_models, mock_transfer_client):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}

    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="in_progress",
        transfer_id="task-1",
    )

    result = sync_data_transfers(test_db)

    assert result.checked == 1
    assert result.completed == 1
    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "pending"


def test_sync_data_transfers_polls_output_and_finalizes(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}

    workflow_run = WorkflowRunFactory.create_sync(
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
    )
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="globus",
        status="in_progress",
        transfer_id="task-1",
    )

    with patch(
        "app.services.globus_transfer.finalize_completed_workflow_run",
        new_callable=AsyncMock,
        return_value=2,
    ) as finalize:
        result = sync_data_transfers(test_db)

    assert result.checked == 1
    assert result.completed == 1
    assert result.finalized_runs == 1
    finalize.assert_awaited_once_with(test_db, workflow_run)


def test_sync_data_transfers_ignores_non_globus_provider(
    test_db, persistent_models, mock_transfer_client
):
    workflow_run = WorkflowRunFactory.create_sync()
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        provider="s3",
        status="pending",
    )

    result = sync_data_transfers(test_db)

    assert result.checked == 0
    mock_transfer_client.get_task.assert_not_called()
    mock_transfer_client.submit_transfer.assert_not_called()


def test_sync_data_transfers_counts_soft_submission_failure(
    test_db, persistent_models, mock_transfer_client
):
    """submit_pending_transfer handles a GlobusAPIError internally (sets status
    to "failed" and returns normally, doesn't raise) - the batch must still
    count it as failed, not submitted."""
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.side_effect = _globus_api_error(
        400, {"code": "UNKNOWN_SCOPE_ERROR", "message": "requested unknown scopes"}
    )

    workflow_run = WorkflowRunFactory.create_sync()
    QueuedJobFactory.create_sync(workflow_run=workflow_run, status="staging")
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="pending",
        source_location="s3://my-bucket/inputs/samplesheets/a.csv",
        destination_location="/test/input/single-prediction/run-1/a.csv",
        transfer_id=None,
    )

    result = sync_data_transfers(test_db)

    assert result.checked == 1
    assert result.submitted == 0
    assert result.failed == 1
    assert result.errored == 0
    queued_job = workflow_run.get_queued_job(test_db)
    assert queued_job.status == "failed"


def test_sync_data_transfers_continues_after_unexpected_error(
    test_db, persistent_models, mock_transfer_client
):
    mock_transfer_client.get_task.side_effect = RuntimeError("boom")

    workflow_run = WorkflowRunFactory.create_sync()
    DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="in_progress",
        transfer_id="task-1",
    )

    result = sync_data_transfers(test_db)

    assert result.checked == 1
    assert result.errored == 1


def test_sync_data_transfers_isolates_failures_between_rows(
    test_db, persistent_models, mock_transfer_client
):
    """One row's unexpected failure must not stop the batch from processing the
    next row - each row gets its own try/except with a rollback in between."""

    def get_task(transfer_id):
        if transfer_id == "task-broken":
            raise RuntimeError("boom")
        return {"status": "SUCCEEDED"}

    mock_transfer_client.get_task.side_effect = get_task

    workflow_run = WorkflowRunFactory.create_sync()
    broken = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="in_progress",
        transfer_id="task-broken",
    )
    healthy = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="globus",
        status="in_progress",
        transfer_id="task-ok",
    )
    # Force a deterministic processing order matching insertion (created_at asc).
    broken.created_at = datetime.now(UTC) - timedelta(minutes=1)
    healthy.created_at = datetime.now(UTC)
    test_db.add_all([broken, healthy])
    test_db.commit()

    result = sync_data_transfers(test_db)

    assert result.checked == 2
    assert result.errored == 1
    assert result.completed == 1

    test_db.refresh(healthy)
    assert healthy.status == "completed"
