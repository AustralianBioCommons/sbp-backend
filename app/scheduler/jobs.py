import asyncio
import os
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from loguru import logger
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models.core import AppUser, DataTransfer
from ..db.models.job_queue import QueuedJob
from ..routes.dependencies import get_db
from ..schemas.workflows.shared import WorkflowName
from ..services import globus_transfer, health, seqera
from ..services.bindflow_executor import launch_bindflow_workflow
from ..services.credits import MONTHLY_CREDIT_REFRESH_ACTOR, SBP_USER_CREDIT_ALLOWANCE
from ..services.job_sync import get_runs_requiring_sync, sync_workflow_runs
from ..services.proteindj_executor import launch_proteindj_workflow
from ..services.proteinfold_executor import launch_proteinfold_workflow
from ..services.seqera import WorkflowLaunchResult
from ..services.seqera_errors import SeqeraAPIError
from ..services.wisps_executor import launch_wisps_workflow
from . import SCHEDULER

LAUNCH_MAX_ATTEMPTS = 3
RETRY_DELAY_BASE = 5 * 60

DATA_TRANSFER_SYNC_BATCH_LIMIT = int(os.getenv("DATA_TRANSFER_SYNC_BATCH_LIMIT", "100"))


class LaunchFunction(Protocol):
    """
    Type annotation for workflow launch functions
    """

    def __call__(
        self,
        *,
        queued_job: QueuedJob,
        settings: Settings,
        dry_run: bool = False,
    ) -> Awaitable[WorkflowLaunchResult | None]: ...


def get_retry_delay(job: QueuedJob) -> timedelta:
    """
    Apply exponential backoff to the retry delay, based on number of attempts.
    """
    return timedelta(seconds=RETRY_DELAY_BASE * (2**job.attempts - 1))


def is_seqera_available(db_session: Session, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    system_status = asyncio.run(health.get_system_status(db_session, settings=settings))
    logger.info(f"System status is {system_status.overall_status}.")
    return system_status.overall_status == "healthy"


def launch_job(job_id: UUID, dry_run: bool = False) -> None:
    logger.info(f"Launching job {job_id}...")
    db_session = next(get_db())
    settings = get_settings()

    ok_to_launch = is_seqera_available(db_session, settings=settings)
    if not ok_to_launch:
        logger.warning("Skipping job launching while system status is unhealthy.")
        return
    job = db_session.get(QueuedJob, job_id)
    if job is None:
        return

    now = datetime.now(tz=UTC)
    launch_func: LaunchFunction
    workflow_name: WorkflowName = cast(WorkflowName, job.workflow.name)
    if workflow_name in ("interaction-screening", "bulk-prediction"):
        launch_func = launch_wisps_workflow
    elif workflow_name in ("single-prediction", "proteinfold"):
        launch_func = launch_proteinfold_workflow
    elif workflow_name in ("de-novo-design", "bindflow", "bindcraft"):
        # de-novo-design covers two algorithms (bindcraft vs rfdiffusion), each
        # with its own executor; workflow_run.tool holds the one selected at launch.
        tool = (job.workflow_run.tool or "").lower()
        launch_func = (
            launch_proteindj_workflow if tool == "rfdiffusion" else launch_bindflow_workflow
        )
    else:
        raise ValueError(f"Unsupported workflow: {job.workflow.name}")
    try:
        result = asyncio.run(launch_func(queued_job=job, settings=settings, dry_run=dry_run))
        if dry_run:
            logger.info("Dry run - not updating job status")
        else:
            if result is not None:
                job.workflow_run.seqera_run_id = result.workflow_id
            job.attempts += 1
            job.status = "submitted"
            job.submitted_at = now
            job.next_attempt_at = None
            job.last_attempt_at = now
            job.error = None
            db_session.add(job)
            db_session.commit()
        return
    except Exception as e:
        logger.error(f"Error launching workflow: {e}")
        if not dry_run:
            job.attempts += 1
            job.error = str(e)
            job.last_attempt_at = now
            if job.attempts >= LAUNCH_MAX_ATTEMPTS:
                job.status = "failed"
                job.next_attempt_at = None
            else:
                job.status = "pending"
                delay = get_retry_delay(job)
                job.next_attempt_at = now + delay
            db_session.add(job)
            db_session.commit()
        return


def get_available_workflow_capacity(settings: Settings | None = None) -> int:
    """
    How many more workflows can be submitted to Gadi right now, per the Seqera API's
    count of workflows still occupying a job slot there (see MAX_CONCURRENT_WORKFLOWS).
    """
    settings = settings or get_settings()
    active_workflow_count = asyncio.run(seqera.count_active_workflows(settings=settings))
    capacity = max(0, settings.seqera.max_concurrent_workflows - active_workflow_count)
    logger.info(
        f"{active_workflow_count}/{settings.seqera.max_concurrent_workflows} workflows active on Gadi "
        f"({capacity} submission slot(s) available)."
    )
    return capacity


def submit_pending_jobs(dry_run: bool = False):
    # Time between jobs
    job_offset = 10
    logger.info("Checking for pending jobs...")
    db_session = next(get_db())
    settings = get_settings()
    ok_to_launch = is_seqera_available(db_session, settings=settings)
    if not ok_to_launch:
        logger.warning("Skipping pending job submission while system status is unhealthy.")
        return

    try:
        available_capacity = get_available_workflow_capacity(settings=settings)
    except SeqeraAPIError as e:
        logger.warning(f"Could not determine Gadi workflow capacity from Seqera: {e}")
        return
    if available_capacity <= 0:
        logger.info("Gadi is at its concurrent workflow limit; skipping submission this tick.")
        return

    now = datetime.now(tz=UTC)

    pending_query = select(QueuedJob).where(
        QueuedJob.status == "pending", QueuedJob.next_attempt_at <= now
    )

    pending_jobs = db_session.scalars(pending_query).all()
    logger.info(f"Found {len(pending_jobs)} pending jobs.")
    jobs_to_submit = pending_jobs[:available_capacity]
    if len(jobs_to_submit) < len(pending_jobs):
        logger.info(
            f"Only submitting {len(jobs_to_submit)} of {len(pending_jobs)} pending jobs "
            "due to available Gadi capacity."
        )
    for index, job in enumerate(jobs_to_submit):
        launch_id = f"launch_job_{job.id}"
        # Ignore if already scheduled
        if SCHEDULER.get_job(launch_id, jobstore="memory") is not None:
            continue

        SCHEDULER.add_job(
            launch_job,
            id=launch_id,
            jobstore="memory",
            kwargs={"job_id": job.id, "dry_run": dry_run},
            name=launch_id,
            max_instances=1,
            replace_existing=True,
            next_run_time=now + timedelta(seconds=index * job_offset),
        )

    logger.info("Finished submitting pending jobs.")


def refresh_user_credits(dry_run: bool = False):
    """Reset every SBP-approved user's credit to the standard monthly allowance.

    Scoped to users who have already been through the one-time bundle grant
    (``sbp_bundle_credit_granted_at IS NOT NULL``) so accounts that merely
    logged in without ever having their workflow-execution role approved
    don't get free credit, and so a role approval landing between refreshes
    can never race the grant's own IS NULL guard (app/routes/dependencies.py).
    """
    db_session = next(get_db())
    approved_filter = AppUser.sbp_bundle_credit_granted_at.is_not(None)
    if dry_run:
        user_count = db_session.scalar(
            select(func.count()).select_from(AppUser).where(approved_filter)
        )
        logger.info(
            f"Dry run - would refresh credit to {SBP_USER_CREDIT_ALLOWANCE} for "
            f"{user_count} approved user(s)."
        )
        return

    result = cast(
        CursorResult,
        db_session.execute(
            update(AppUser)
            .where(approved_filter)
            .values(
                credit=SBP_USER_CREDIT_ALLOWANCE,
                credit_updated_at=datetime.now(UTC),
                credit_updated_by=MONTHLY_CREDIT_REFRESH_ACTOR,
            )
        ),
    )
    db_session.commit()
    logger.info(
        f"Refreshed credit to {SBP_USER_CREDIT_ALLOWANCE} for {result.rowcount} approved "
        "user(s)."
    )


def sync_completed_workflow_runs(dry_run: bool = False):
    logger.info("Checking for completed workflow runs to sync...")
    db_session = next(get_db())
    settings = get_settings()
    if dry_run:
        runs = get_runs_requiring_sync(db_session, limit=settings.seqera.workflow_sync_batch_limit)
        logger.info(f"Dry run - found {len(runs)} workflow run(s) requiring sync.")
        return

    ok_to_sync = is_seqera_available(db_session, settings=settings)
    if not ok_to_sync:
        logger.warning("Skipping workflow run result sync while system status is unhealthy.")
        return

    result = asyncio.run(
        sync_workflow_runs(
            db_session,
            limit=settings.seqera.workflow_sync_batch_limit,
            settings=settings,
        )
    )
    logger.info(
        "Finished syncing workflow runs: "
        f"checked={result.checked}, completed={result.completed}, "
        f"skipped={result.skipped}, errored={result.errored}."
    )
    for run_result in result.results:
        if run_result.error is None:
            continue
        logger.warning(
            "Workflow run sync failed: "
            f"run_id={run_result.run_id}, "
            f"seqera_run_id={run_result.seqera_run_id}, "
            f"error={run_result.error}"
        )


def sync_data_transfers(dry_run: bool = False):
    """Submit pending and poll in-progress Globus data transfers, notifying the
    workflow launcher (flips QueuedJob "staging" -> "pending"/"failed") once a
    run's input staging settles."""
    logger.info("Checking for Globus data transfers to sync...")
    db_session = next(get_db())
    if dry_run:
        pending_count = db_session.scalar(
            select(func.count())
            .select_from(DataTransfer)
            .where(
                DataTransfer.provider == "globus",
                DataTransfer.status.in_(["pending", "in_progress"]),
            )
        )
        logger.info(f"Dry run - found {pending_count} Globus data transfer(s) requiring sync.")
        return

    result = globus_transfer.sync_data_transfers(db_session, limit=DATA_TRANSFER_SYNC_BATCH_LIMIT)
    logger.info(
        "Finished syncing Globus data transfers: "
        f"checked={result.checked}, submitted={result.submitted}, "
        f"completed={result.completed}, failed={result.failed}, errored={result.errored}."
    )
