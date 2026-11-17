"""Native S3 provenance, managed Globus movement, and backend-isolated workers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.db.models.core import DataTransfer, RunInput, RunOutput
from app.schemas.backend import ComputeBackendName, DataTransferMode, RepositoryTransferMode
from app.services import globus_transfer, job_sync
from app.services.data_movement import DataMovement
from app.services.s3 import S3ServiceError, S3UploadResult
from tests.datagen import (
    AppUserFactory,
    ComputeBackendFactory,
    DataTransferFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


@pytest.fixture
def movement_run(persistent_models):
    backend = ComputeBackendFactory.create_sync(
        name=ComputeBackendName.SETONIX,
        data_transfer=DataTransferMode.NATIVE,
        repository_transfer=RepositoryTransferMode.NATIVE,
        env_prefix="UNCONFIGURED_",
    )
    workflow = WorkflowFactory.create_sync(name="de-novo-design", tool="bindcraft")
    return WorkflowRunFactory.create_sync(
        owner=AppUserFactory.create_sync(),
        workflow=workflow,
        backend=backend,
        tool="bindcraft",
        seqera_run_id="native-data-run",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
    )


@pytest.mark.parametrize("mode", list(DataTransferMode))
@pytest.mark.parametrize("repo_mode", list(RepositoryTransferMode))
async def test_inputs_and_samplesheets_follow_data_mode(
    test_db,
    movement_run,
    mock_settings,
    mocker,
    mode,
    repo_mode,
):
    run = movement_run
    run.backend.data_transfer = mode
    run.backend.repository_transfer = repo_mode
    # Supply backend settings only for managed movement; native inputs never load them.
    settings_loader = mocker.patch.object(run.backend, "get_settings")
    settings_loader.return_value.globus = mock_settings.globus
    movement = DataMovement.for_run(run, mock_settings)
    source = "s3://test-s3-bucket/inputs/query.fa"
    rows = [{"sequence": source, "group": "g1"}, {"sequence": source, "group": "g2"}]
    upload = mocker.patch(
        "app.services.data_movement.upload_samplesheet_rows_to_s3", new_callable=AsyncMock
    )
    upload.return_value = S3UploadResult(
        success=True, file_key="inputs/rewritten.csv", bucket="test-s3-bucket"
    )
    result = await movement.prepare_samplesheet(
        test_db, run.id, run.workflow.name, "inputs/original.csv", rows, "sequence"
    )
    assert rows[0]["sequence"] == source
    assert rows[1]["sequence"] == source
    assert test_db.scalar(select(func.count()).select_from(RunInput)) == 1
    provenance = test_db.scalar(select(RunInput))
    assert provenance.s3_object.uri == source
    if mode == DataTransferMode.NATIVE:
        assert result == "inputs/original.csv"
        assert provenance.data_transfer_id is None
        assert test_db.scalar(select(func.count()).select_from(DataTransfer)) == 0
        assert (
            movement.output_location(run.id, run.workflow.name) == f"s3://test-s3-bucket/{run.id}"
        )
        upload.assert_not_awaited()
        settings_loader.assert_not_called()
    else:
        assert result == "inputs/rewritten.csv"
        assert test_db.scalar(select(func.count()).select_from(DataTransfer)) == 1
        assert (
            provenance.data_transfer.destination_location
            == f"/test/input/de-novo-design/{run.id}/query.fa"
        )
        assert (
            upload.call_args.args[0][0]["sequence"] == provenance.data_transfer.destination_location
        )
        assert (
            movement.output_location(run.id, run.workflow.name)
            == f"/test/output/de-novo-design/{run.id}"
        )


@pytest.mark.parametrize("available", ["none", "partial", "complete"])
async def test_native_results_require_required_categories_without_transfers(
    test_db,
    movement_run,
    mock_settings,
    mocker,
    available,
):
    run = movement_run
    keys = [f"{run.id}/results/ranked_designs.csv", f"{run.id}/results/ranked_designs.tar.gz"]
    files = [] if available == "none" else keys[:1] if available == "partial" else keys
    mocker.patch(
        "app.services.results_utils.list_s3_files",
        new=AsyncMock(return_value=[{"key": key} for key in files]),
    )
    mocker.patch("app.services.job_sync.ensure_completed_run_score", new=AsyncMock())
    mocker.patch("app.services.job_sync.sync_service_usage", new=AsyncMock())
    result = await job_sync.sync_workflow_run(test_db, run, settings=mock_settings)
    assert result.sync_completed == (available == "complete")
    assert (run.sync_completed_at is not None) == (available == "complete")
    assert test_db.scalar(select(func.count()).select_from(DataTransfer)) == 0
    outputs = test_db.scalars(select(RunOutput)).all()
    assert len(outputs) == len(files)
    assert all(output.data_transfer_id is None for output in outputs)


async def test_native_s3_failure_remains_retryable(test_db, movement_run, mock_settings, mocker):
    mocker.patch(
        "app.services.results_utils.list_s3_files",
        new=AsyncMock(side_effect=S3ServiceError("S3 unavailable")),
    )
    with pytest.raises(S3ServiceError, match="S3 unavailable"):
        await job_sync.sync_workflow_run(
            test_db, movement_run, settings=mock_settings, suppress_s3_errors=True
        )
    assert movement_run.sync_completed_at is None
    assert test_db.scalar(select(func.count()).select_from(DataTransfer)) == 0


async def test_native_force_resync_only_reindexes_s3(test_db, movement_run, mock_settings, mocker):
    run = movement_run
    files = [f"{run.id}/results/ranked_designs.csv", f"{run.id}/results/ranked_designs.tar.gz"]
    mocker.patch(
        "app.services.results_utils.list_s3_files",
        new=AsyncMock(return_value=[{"key": key} for key in files]),
    )
    mocker.patch("app.services.job_sync.ensure_completed_run_score", new=AsyncMock())
    mocker.patch("app.services.job_sync.sync_service_usage", new=AsyncMock())
    cleanup = mocker.patch("app.services.job_sync.check_any_output_transfer_cleaned_up")
    reset = mocker.patch("app.services.job_sync.reset_completed_output_transfers")
    result = await job_sync.force_resync_run_outputs(test_db, run, settings=mock_settings)
    assert result.ready
    assert result.outputs_synced == 2
    cleanup.assert_not_called()
    reset.assert_not_called()
    assert test_db.scalar(select(func.count()).select_from(DataTransfer)) == 0


@pytest.mark.parametrize(
    "phase",
    [
        "submit_input",
        "poll_input",
        "submit_output",
        "poll_output",
        "submit_cleanup",
        "poll_cleanup",
    ],
)
def test_workers_use_each_transfers_backend_settings(
    test_db,
    persistent_models,
    mock_settings,
    monkeypatch,
    mocker,
    phase,
):
    clients = {}
    transfers = []
    for name in ComputeBackendName:
        prefix = f"MOVE_{name.value.upper()}_"
        values = mock_settings.globus.model_dump() | {
            "client_id": f"client-{name.value}",
            "backend_collection_id": f"collection-{name.value}",
            "collection_root": f"/{name.value}",
            "input_dir": f"/{name.value}/input",
            "output_dir": f"/{name.value}/output",
        }
        for key, value in values.items():
            monkeypatch.setenv(f"{prefix}GLOBUS_{key.upper()}", str(value))
        backend = ComputeBackendFactory.create_sync(name=name, env_prefix=prefix)
        run = WorkflowRunFactory.create_sync(
            backend=backend, owner=AppUserFactory.create_sync(), seqera_final_status="SUCCEEDED"
        )
        cleanup = phase.endswith("cleanup")
        direction = "input" if phase.endswith("input") else "output"
        transfer = DataTransferFactory.create_sync(
            workflow_run=run,
            provider="globus",
            direction=direction,
            source_location=(
                f"s3://test-s3-bucket/inputs/{name.value}.fa"
                if direction == "input"
                else f"/{name.value}/output/existing/file.txt"
            ),
            destination_location=(
                f"/{name.value}/input/existing/file.fa"
                if direction == "input"
                else f"s3://test-s3-bucket/{run.id}/file.txt"
            ),
            status=(
                "completed"
                if cleanup
                else "pending" if phase.startswith("submit") else "in_progress"
            ),
            transfer_id=f"existing-{name.value}",
            cleanup_status=(
                ("pending" if phase.startswith("submit") else "in_progress") if cleanup else None
            ),
            cleanup_transfer_id=f"delete-{name.value}" if cleanup else None,
        )
        transfers.append((transfer, transfer.source_location, transfer.destination_location))
        client = MagicMock()
        client.get_task.return_value = {"status": "SUCCEEDED"}
        client.submit_transfer.return_value = {"task_id": f"task-{name.value}"}
        client.submit_delete.return_value = {"task_id": f"delete-task-{name.value}"}
        clients[f"client-{name.value}"] = client
    client_factory = mocker.patch(
        "app.services.globus_transfer.get_transfer_client",
        side_effect=lambda config: clients[config.client_id],
    )
    worker = (
        globus_transfer.sync_data_cleanups
        if phase.endswith("cleanup")
        else globus_transfer.sync_data_transfers
    )
    result = worker(test_db)
    assert result.checked == 2
    assert result.errored == 0
    assert {call.args[0].client_id for call in client_factory.call_args_list} == set(clients)
    for name, (transfer, source, destination) in zip(ComputeBackendName, transfers, strict=True):
        assert transfer.source_location == source
        assert transfer.destination_location == destination
        client = clients[f"client-{name.value}"]
        client.get_submission_id.assert_not_called()
        if phase.startswith("poll"):
            client.get_task.assert_called_once_with(
                f"delete-{name.value}" if phase.endswith("cleanup") else f"existing-{name.value}"
            )
        elif phase.endswith("cleanup"):
            delete_data = client.submit_delete.call_args.args[0]
            assert delete_data["endpoint"] == f"collection-{name.value}"
            assert delete_data["submission_id"] == f"delete-{name.value}"
        else:
            transfer_data = client.submit_transfer.call_args.args[0]
            assert transfer_data["submission_id"] == f"existing-{name.value}"
            assert (
                transfer_data[
                    "destination_endpoint" if phase.endswith("input") else "source_endpoint"
                ]
                == f"collection-{name.value}"
            )
