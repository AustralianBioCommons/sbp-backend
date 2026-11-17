"""Shared readiness checks for queued workflows."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import DataTransfer
from ..db.models.job_queue import QueuedJob


def promote_ready_job(db: Session, queued_job: QueuedJob, *, commit: bool = True) -> None:
    """
    Update staging job to pending once:
     - all of its input DataTransfers completed
     - workflow's repo staging completed (if needed).
    If these have failed, mark the job as failed
    """
    if queued_job.status != "staging":
        return

    input_transfers = db.scalars(
        select(DataTransfer).where(
            DataTransfer.workflow_run_id == queued_job.workflow_run_id,
            DataTransfer.direction == "input",
        )
    ).all()
    failed_transfer = next((t for t in input_transfers if t.status == "failed"), None)
    if failed_transfer is not None:
        queued_job.status = "failed"
        queued_job.error = f"Input staging failed: {failed_transfer.error_message}"
        db.add(queued_job)
        if commit:
            db.commit()
        return
    if not all(transfer.status == "completed" for transfer in input_transfers):
        return

    if not queued_job.refresh_repository_readiness(db):
        db.add(queued_job)
        if commit:
            db.commit()
        return

    queued_job.status = "pending"
    # Make sure the pending job has next_attempt_at set so it gets picked up
    queued_job.next_attempt_at = datetime.now(tz=UTC)
    db.add(queued_job)
    if commit:
        db.commit()
