"""Globus transfer submission, polling, and launch-gate notification."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import globus_sdk
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import GlobusSettings, get_settings
from ..db.models.core import DataTransfer, DataTransferStatus
from ..db.models.job_queue import QueuedJob
from .globus_client import get_transfer_client
from .globus_errors import GlobusConfigurationError, GlobusTransferError

logger = logging.getLogger(__name__)

# How long a transfer may sit in Globus's INACTIVE state (collection re-activation/
# consent needed, which won't resolve on its own) before it's given up on.
STALE_TRANSFER_TIMEOUT = timedelta(minutes=30)

_GLOBUS_TASK_STATUS_MAP: dict[str, DataTransferStatus] = {
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "ACTIVE": "in_progress",
    "INACTIVE": "failed",
}


def build_gadi_input_path(
    run_id: object,
    workflow_name: str,
    filename: str,
    *,
    globus_settings: GlobusSettings | None = None,
) -> str:
    """Build the Gadi-local destination path for a staged input file."""
    globus_settings = globus_settings or get_settings().globus
    return f"{globus_settings.input_dir}/{workflow_name}/{run_id}/{filename}"


def build_gadi_output_path(
    run_id: object,
    workflow_name: str,
    filename: str | None = None,
    *,
    globus_settings: GlobusSettings | None = None,
) -> str:
    """Build the Gadi-local output directory or child path for a workflow run."""
    globus_settings = globus_settings or get_settings().globus
    run_output_dir = f"{globus_settings.output_dir}/{workflow_name}/{run_id}"
    if filename is None:
        return run_output_dir
    return f"{run_output_dir}/{filename}"


def _s3_relative_path(source_location: str) -> str:
    """Path relative to the S3 Globus collection root for a ``s3://bucket/key`` URI.

    The collection's root maps 1:1 to the bucket root
    """
    if not source_location.startswith("s3://"):
        raise GlobusTransferError(f"Not an S3 URI: {source_location}")
    remainder = source_location.removeprefix("s3://")
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise GlobusTransferError(f"S3 URI missing a bucket or object key: {source_location}")
    return f"/{key}"


def _gadi_relative_path(
    destination_location: str, *, globus_settings: GlobusSettings | None = None
) -> str:
    """Path relative to the Gadi Globus collection root for a real absolute Gadi path.

    The collection's root maps to GLOBUS_GADI_COLLECTION_ROOT on the real filesystem, not
    "/" (confirmed in production: sending the full absolute path unchanged
    double-nests it under the collection root and the destination never appears at
    the expected real path).
    """
    globus_settings = globus_settings or get_settings().globus
    collection_root = globus_settings.gadi_collection_root
    if not destination_location.startswith(collection_root + "/"):
        raise GlobusTransferError(
            f"Destination path {destination_location} is not under the Gadi "
            f"collection root {collection_root}"
        )
    # The startswith check above guarantees this always begins with "/".
    return destination_location[len(collection_root) :]


@dataclass(frozen=True)
class DataTransferSyncResult:
    """Outcome for one batch of Globus data-transfer sync work."""

    checked: int
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    errored: int = 0


def submit_pending_transfer(
    db: Session,
    data_transfer: DataTransfer,
    *,
    globus_settings: GlobusSettings | None = None,
) -> None:
    """Submit a Globus transfer for a ``pending`` DataTransfer row."""
    globus_settings = globus_settings or get_settings().globus
    transfer_client = get_transfer_client(globus_settings)
    direction = data_transfer.direction
    match direction:
        case "input":
            source_collection = globus_settings.s3_collection_id
            source_path = _s3_relative_path(data_transfer.source_location)
            destination_collection = globus_settings.gadi_collection_id
            destination_path = _gadi_relative_path(
                data_transfer.destination_location, globus_settings=globus_settings
            )
        case "output":
            source_collection = globus_settings.gadi_collection_id
            source_path = _gadi_relative_path(
                data_transfer.source_location, globus_settings=globus_settings
            )
            destination_collection = globus_settings.s3_collection_id
            destination_path = _s3_relative_path(data_transfer.destination_location)
        case _:
            raise ValueError(f"Invalid direction: {direction}")

    # Persist the submission_id (temporarily held in transfer_id) before calling
    # submit_transfer, and reuse it on retry: Globus dedupes submissions on this
    # id, so a crash between submit and our own status-update commit must not
    # mint a fresh id and double-submit. transfer_id is overwritten with the
    # real Globus task id below once submission succeeds.
    submission_id = data_transfer.transfer_id
    if not submission_id:
        submission_id = cast(str, transfer_client.get_submission_id()["value"])
        data_transfer.transfer_id = submission_id
        db.add(data_transfer)
        db.commit()

    try:
        transfer_data = globus_sdk.TransferData(
            source_endpoint=source_collection,
            destination_endpoint=destination_collection,
            submission_id=submission_id,
            label=f"sbp-run-{data_transfer.workflow_run_id}",
        )
        transfer_data.add_item(
            source_path,
            destination_path,
            recursive=data_transfer.recursive,
        )
        result = transfer_client.submit_transfer(transfer_data)
    except globus_sdk.GlobusAPIError as exc:
        # Covers both TransferAPIError (submission rejected) and AuthAPIError
        # (token/scope request rejected, e.g. UNKNOWN_SCOPE_ERROR) - both are
        # structured API errors that won't resolve by retrying unchanged. Once
        # failed, this row is never retried (sync_data_transfers only re-queries
        # pending/in_progress), so the stashed submission id is no longer
        # meaningful - clear it back to None.
        logger.warning("Globus transfer submission failed for %s: %s", data_transfer.id, exc)
        data_transfer.status = "failed"
        data_transfer.transfer_id = None
        data_transfer.error_message = str(exc)
        data_transfer.updated_at = datetime.now(UTC)
        db.add(data_transfer)
        db.commit()
        return

    data_transfer.transfer_id = result["task_id"]
    data_transfer.status = "in_progress"
    data_transfer.updated_at = datetime.now(UTC)
    db.add(data_transfer)
    db.commit()


def poll_transfer(
    db: Session,
    data_transfer: DataTransfer,
    *,
    globus_settings: GlobusSettings | None = None,
) -> None:
    """Poll Globus for an ``in_progress`` DataTransfer row's task status."""
    if not data_transfer.transfer_id:
        raise GlobusTransferError(f"DataTransfer {data_transfer.id} has no transfer_id to poll")

    globus_settings = globus_settings or get_settings().globus
    transfer_client = get_transfer_client(globus_settings)
    try:
        task = transfer_client.get_task(data_transfer.transfer_id)
    except globus_sdk.GlobusAPIError as exc:
        # A poll failure (e.g. an auth/scope error) doesn't mean the underlying
        # Globus transfer failed - it may still be progressing server-side and we
        # simply can't observe it right now. Record the error for visibility
        # without touching status, so it keeps retrying on the next tick.
        data_transfer.error_message = f"Poll failed: {exc}"
        data_transfer.updated_at = datetime.now(UTC)
        db.add(data_transfer)
        db.commit()
        raise GlobusTransferError(f"Failed to poll Globus transfer: {exc}") from exc

    globus_status = task["status"]
    if globus_status == "INACTIVE":
        # SQLite (used in tests) returns naive datetimes even for DateTime(timezone=True)
        # columns, unlike Postgres - normalize so this subtraction works on both.
        created_at = data_transfer.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - created_at
        if age > STALE_TRANSFER_TIMEOUT:
            data_transfer.status = "failed"
            data_transfer.error_message = (
                "Transfer stuck INACTIVE - Globus collection activation/consent required"
            )
            data_transfer.updated_at = datetime.now(UTC)
            db.add(data_transfer)
            db.commit()
        return

    new_status = _GLOBUS_TASK_STATUS_MAP.get(globus_status, "in_progress")
    if new_status == data_transfer.status:
        return

    data_transfer.status = new_status
    data_transfer.updated_at = datetime.now(UTC)
    if new_status == "failed":
        fatal_error = task.get("fatal_error") or {}
        data_transfer.error_message = fatal_error.get("description") or "Globus transfer failed"
    db.add(data_transfer)
    db.commit()


def reset_failed_output_transfers(
    db: Session,
    *,
    transfer_ids: Iterable[UUID] | None = None,
    workflow_run_id: UUID | None = None,
) -> int:
    """Reset failed Globus output transfers so the sync worker can retry them."""
    conditions = [
        DataTransfer.provider == "globus",
        DataTransfer.direction == "output",
        DataTransfer.status == "failed",
    ]
    if transfer_ids is not None:
        transfer_ids = list(transfer_ids)
        if not transfer_ids:
            return 0
        conditions.append(DataTransfer.id.in_(transfer_ids))
    if workflow_run_id is not None:
        conditions.append(DataTransfer.workflow_run_id == workflow_run_id)

    transfers = list(db.scalars(select(DataTransfer).where(*conditions)))
    for data_transfer in transfers:
        data_transfer.reset_to_pending(session=db, commit=False)
    if transfers:
        db.commit()
    return len(transfers)


def _notify_launcher(db: Session, data_transfer: DataTransfer) -> None:
    """After an input transfer settles, try to move the run's QueuedJob out of
    "staging".

    Observed once in production: all input transfers for a run showed
    ``completed`` minutes before Nextflow started, yet the first process still
    failed with Singularity reporting the staged input directory didn't exist
    to mount, though it existed both before and after. Timestamps ruled out a
    race in this gating logic - looked like a transient negative-cache/
    automount hiccup on Gadi's ``/g/data`` mount on that compute node. Revisit
    if it recurs.
    """
    if data_transfer.direction != "input" or data_transfer.status not in (
        "completed",
        "failed",
    ):
        return

    queued_job = data_transfer.workflow_run.get_queued_job(db)
    if queued_job is None:
        return
    _try_promote_staging_job(db, queued_job)


def _try_promote_staging_job(db: Session, queued_job: QueuedJob) -> None:
    """Flip a "staging" QueuedJob to "pending" once every gate clears: all of
    its input DataTransfers completed, and its workflow's repo staging (if
    applicable - see workflow_repo_staging.py) completed. Fails the job
    immediately if any gate reports failure, rather than leaving it stuck.

    Called both after an input transfer settles (_notify_launcher, above) and
    after a workflow's repo staging settles (workflow_repo_staging.py) - either
    event can be the last one a given run was waiting on.
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
        db.commit()
        return
    if not all(transfer.status == "completed" for transfer in input_transfers):
        return

    workflow = queued_job.workflow
    if workflow.repo_staging_status == "failed":
        queued_job.status = "failed"
        queued_job.error = f"Workflow repo staging failed: {workflow.repo_staging_error_message}"
        db.add(queued_job)
        db.commit()
        return
    if workflow.repo_staging_status is not None and workflow.repo_staging_status != "completed":
        return

    queued_job.status = "pending"
    db.add(queued_job)
    db.commit()


def sync_data_transfers(
    db: Session,
    *,
    limit: int = 100,
    globus_settings: GlobusSettings | None = None,
) -> DataTransferSyncResult:
    """Submit pending and poll in-progress Globus data transfers for one batch."""
    globus_settings = globus_settings or get_settings().globus
    stmt = (
        select(DataTransfer)
        .where(
            DataTransfer.provider == "globus",
            DataTransfer.status.in_(["pending", "in_progress"]),
        )
        .order_by(DataTransfer.created_at.asc())
        .limit(limit)
    )
    transfers = list(db.scalars(stmt))

    submitted = completed = failed = errored = 0
    for data_transfer in transfers:
        try:
            if data_transfer.status == "pending":
                submit_pending_transfer(db, data_transfer, globus_settings=globus_settings)
                if data_transfer.status == "in_progress":
                    submitted += 1
            else:
                poll_transfer(db, data_transfer, globus_settings=globus_settings)
            _notify_launcher(db, data_transfer)
        except (GlobusConfigurationError, GlobusTransferError) as exc:
            db.rollback()
            logger.warning("Failed to sync Globus data transfer %s: %s", data_transfer.id, exc)
            errored += 1
            continue
        except Exception:
            db.rollback()
            logger.exception("Unexpected error syncing Globus data transfer %s", data_transfer.id)
            errored += 1
            continue

        if data_transfer.status == "failed":
            failed += 1
        elif data_transfer.status == "completed":
            completed += 1

    return DataTransferSyncResult(
        checked=len(transfers),
        submitted=submitted,
        completed=completed,
        failed=failed,
        errored=errored,
    )
