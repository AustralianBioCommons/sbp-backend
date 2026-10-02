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


def test_job_queue_allows_prerun_script(test_db, persistent_models):
    """
    preRunScript is now baked into launch_payload at prepare time (no secrets
    live in prerun scripts anymore), so persisting it must succeed.
    """
    workflow_run = WorkflowRunFactory.create_sync()
    job = QueuedJobFactory.create_sync(
        workflow_run=workflow_run,
        launch_payload={"preRunScript": "echo 'hello world'"},
    )
    assert job.launch_payload["preRunScript"] == "echo 'hello world'"
