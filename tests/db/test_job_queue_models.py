import pytest

from tests.datagen import QueuedJobFactory, WorkflowRunFactory


def test_job_queue_model(test_db, persistent_models):
    """
    Test that the QueuedJob model is correctly created in the database.
    """
    workflow_run = WorkflowRunFactory.create_sync()
    job = QueuedJobFactory.create_sync(workflow_run=workflow_run)
    assert job.id is not None
    assert job.workflow_run_id is not None
    assert job.queued_at is not None


def test_job_queue_no_prerun_script(test_db, persistent_models):
    """
    Test that the QueuedJob model does not allow launch_payload to include a preRunScript.
    """
    workflow_run = WorkflowRunFactory.create_sync()
    with pytest.raises(ValueError):
        QueuedJobFactory.create_sync(
            workflow_run=workflow_run,
            launch_payload={"preRunScript": "echo 'hello world'"}
        )
