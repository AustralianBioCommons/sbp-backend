import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.models import DataTransfer, WorkflowRun
from tests.datagen import (
    DataTransferFactory,
    RunInputFactory,
    RunOutputFactory,
    S3ObjectFactory,
    WorkflowRunFactory,
)


def test_data_transfer_model_structure():
    """Test DataTransfer model structure and relationships."""
    assert DataTransfer.__tablename__ == "data_transfers"

    mapper = inspect(DataTransfer)
    column_names = {col.key for col in mapper.columns}
    assert "id" in column_names
    assert "workflow_run_id" in column_names
    assert "direction" in column_names
    assert "provider" in column_names
    assert "source_location" in column_names
    assert "destination_location" in column_names
    assert "transfer_id" in column_names
    assert "status" in column_names
    assert "provider_metadata" in column_names
    assert "created_at" in column_names
    assert "updated_at" in column_names
    assert "error_message" in column_names

    assert hasattr(DataTransfer, "workflow_run")
    assert hasattr(WorkflowRun, "data_transfers")


def test_data_transfer_persists_input_and_output_records(test_db, persistent_models):
    """A workflow run can be associated with multiple data transfer records."""
    workflow_run = WorkflowRunFactory.create_sync()

    inbound = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="input",
        provider="s3",
        source_location="s3://bucket/input.txt",
        destination_location="/work/input.txt",
        status="completed",
        transfer_id="xfer-1",
        error_message=None,
    )
    outbound = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        direction="output",
        provider="s3",
        source_location="/work/output.txt",
        destination_location="s3://bucket/output.txt",
        status="pending",
        transfer_id=None,
        error_message=None,
    )

    assert inbound.id is not None
    assert outbound.id is not None
    assert inbound.workflow_run_id == workflow_run.id
    assert outbound.workflow_run_id == workflow_run.id
    assert inbound.created_at is not None

    test_db.refresh(workflow_run)
    transfer_ids = {transfer.id for transfer in workflow_run.data_transfers}
    assert transfer_ids == {inbound.id, outbound.id}


def test_data_transfer_records_error_message(test_db, persistent_models):
    """A failed transfer can record an error message and be updated later."""
    workflow_run = WorkflowRunFactory.create_sync()

    transfer = DataTransferFactory.create_sync(
        workflow_run=workflow_run,
        status="failed",
        error_message="connection timed out",
        updated_at=None,
    )
    assert transfer.status == "failed"
    assert transfer.error_message == "connection timed out"
    assert transfer.updated_at is None


def test_run_input_links_to_data_transfer(test_db, persistent_models):
    """A RunInput row can be linked to the DataTransfer record it originated from."""
    workflow_run = WorkflowRunFactory.create_sync()
    s3_object = S3ObjectFactory.create_sync()
    transfer = DataTransferFactory.create_sync(workflow_run=workflow_run, direction="input")

    run_input = RunInputFactory.create_sync(
        run_id=workflow_run.id,
        s3_object_id=s3_object.object_key,
        data_transfer=transfer,
    )

    assert run_input.data_transfer_id == transfer.id
    test_db.refresh(transfer)
    assert transfer.run_input.run_id == workflow_run.id
    assert transfer.run_output is None


def test_run_output_links_to_data_transfer(test_db, persistent_models):
    """A RunOutput row can be linked to the DataTransfer record it originated from."""
    workflow_run = WorkflowRunFactory.create_sync()
    s3_object = S3ObjectFactory.create_sync()
    transfer = DataTransferFactory.create_sync(workflow_run=workflow_run, direction="output")

    run_output = RunOutputFactory.create_sync(
        run_id=workflow_run.id,
        s3_object_id=s3_object.object_key,
        data_transfer=transfer,
    )

    assert run_output.data_transfer_id == transfer.id
    test_db.refresh(transfer)
    assert transfer.run_output.run_id == workflow_run.id
    assert transfer.run_input is None


def test_run_input_requires_data_transfer(test_db, persistent_models):
    """Every RunInput/RunOutput row must be linked to a DataTransfer record."""
    workflow_run = WorkflowRunFactory.create_sync()
    s3_object = S3ObjectFactory.create_sync()

    with pytest.raises(IntegrityError):
        RunInputFactory.create_sync(
            run_id=workflow_run.id, s3_object_id=s3_object.object_key, data_transfer=None
        )
    test_db.rollback()

    with pytest.raises(IntegrityError):
        RunOutputFactory.create_sync(
            run_id=workflow_run.id, s3_object_id=s3_object.object_key, data_transfer=None
        )
    test_db.rollback()
