import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Protocol, cast
from uuid import UUID

from loguru import logger
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models.core import AppUser, DataTransfer, Workflow
from ..db.models.job_queue import QueuedJob
from ..routes.dependencies import get_db
from ..schemas.workflows.shared import WorkflowName
from ..services import globus_transfer, health, seqera, workflow_repo_staging
from ..services.bindflow_executor import launch_bindflow_workflow
from ..services.credits import MONTHLY_CREDIT_REFRESH_ACTOR, SBP_USER_CREDIT_ALLOWANCE
from ..services.job_sync import get_runs_requiring_sync, sync_workflow_runs
from ..services.proteindj_executor import launch_proteindj_workflow
from ..services.proteinfold_executor import launch_proteinfold_workflow
from ..services.seqera import WorkflowLaunchResult
from ..services.seqera_errors import SeqeraAPIError
from ..services.wisps_executor import launch_wisps_workflow
from . import SCHEDULER

LAUNCH_MAX_ATTEMPTS = 5
RETRY_DELAY_BASE = 5 * 60
# Keeps retries from colliding with other 5-min-cadence scheduler jobs.
RETRY_DELAY_JITTER_SECONDS = 120

DATA_TRANSFER_SYNC_BATCH_LIMIT = int(os.getenv("DATA_TRANSFER_SYNC_BATCH_LIMIT", "100"))


def with_scheduler_db_session[SchedulerJob: Callable[..., object]](
    func: SchedulerJob,
) -> SchedulerJob:
    """
    Decorator to run jobs with a database session. Automatically
    closes the DB session.
    """

    @wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        provided_db_session = kwargs.pop("db_session", None)
        if provided_db_session is not None:
            return func(*args, db_session=provided_db_session, **kwargs)

        db_context = get_db()
        db_session = next(db_context)
        try:
            return func(*args, db_session=db_session, **kwargs)
        finally:
            db_context.close()

    return cast(SchedulerJob, wrapper)


def require_scheduler_db_session(db_session: Session | None) -> Session:
    if db_session is None:
        raise RuntimeError("Scheduler job called without a database session")
    return db_session


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
    """Exponential backoff plus jitter (see RETRY_DELAY_JITTER_SECONDS)."""
    base_delay = RETRY_DELAY_BASE * (2**job.attempts - 1)
    jitter = random.uniform(0, RETRY_DELAY_JITTER_SECONDS)
    return timedelta(seconds=base_delay + jitter)


def is_seqera_available(db_session: Session, settings: Settings | None = None) -> bool:
    """Gate job submission on the Seqera health status.

    Normally reads the shared cache only - never runs a live probe itself.
    Keeping the cache fresh is the dedicated refresh_seqera_health_status
    scheduler job's responsibility, so a slow/hung Seqera probe can no longer
    stall every submit_pending_jobs/launch_job tick the way an inline check
    would. The only exception is a cold cache (e.g. right after a fresh
    deploy, before that job's first run): assuming healthy there would let
    submission proceed with zero actual signal on Seqera's state, so this
    falls back to a one-off live probe (which also populates the cache for
    the next call) rather than guessing.
    """
    settings = settings or get_settings()
    if settings.seqera.skip_health_gate:
        logger.warning(
            "Skipping Seqera health gate (SEQERA_SKIP_HEALTH_GATE=true) - "
            "local testing only, do not enable in a real environment."
        )
        return True
    cached_status = health.get_cached_system_status(db_session)
    if cached_status is not None:
        logger.info(f"System status is {cached_status.overall_status} (cached).")
        return cached_status.overall_status == "healthy"

    logger.info("No cached Seqera health status yet; running a live probe.")
    system_status = asyncio.run(health.get_system_status(db_session, settings=settings))
    logger.info(f"System status is {system_status.overall_status}.")
    return system_status.overall_status == "healthy"


@with_scheduler_db_session
def refresh_seqera_health_status(
    dry_run: bool = False, *, db_session: Session | None = None
) -> None:
    """Actively refresh the shared Seqera health cache on its own schedule.

    This is the only place that runs the live health probes - is_seqera_available
    (used by submit_pending_jobs/launch_job/sync_completed_workflow_runs) only
    ever reads the cache it writes, decoupling job-submission ticks from Seqera
    probe latency entirely.
    """
    db_session = require_scheduler_db_session(db_session)
    if dry_run:
        logger.info("Dry run - not probing Seqera or refreshing the health cache")
        return
    settings = get_settings()
    status = asyncio.run(health.refresh_db_cache(db_session, settings=settings))
    logger.info(f"Refreshed Seqera health status: {status.overall_status}.")


@with_scheduler_db_session
def launch_job(job_id: UUID, dry_run: bool = False, *, db_session: Session | None = None) -> None:
    db_session = require_scheduler_db_session(db_session)
    logger.info(f"Launching job {job_id}...")
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


@with_scheduler_db_session
def submit_pending_jobs(dry_run: bool = False, *, db_session: Session | None = None):
    db_session = require_scheduler_db_session(db_session)
    # Time between jobs
    job_offset = 10
    logger.info("Checking for pending jobs...")
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


@with_scheduler_db_session
def refresh_user_credits(dry_run: bool = False, *, db_session: Session | None = None):
    """Reset every SBP-approved user's credit to the standard monthly allowance.

    Scoped to users who have already been through the one-time bundle grant
    (``sbp_bundle_credit_granted_at IS NOT NULL``) so accounts that merely
    logged in without ever having their workflow-execution role approved
    don't get free credit, and so a role approval landing between refreshes
    can never race the grant's own IS NULL guard (app/routes/dependencies.py).
    """
    db_session = require_scheduler_db_session(db_session)
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


@with_scheduler_db_session
def sync_completed_workflow_runs(dry_run: bool = False, *, db_session: Session | None = None):
    db_session = require_scheduler_db_session(db_session)
    logger.info("Checking for completed workflow runs to sync...")
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


@with_scheduler_db_session
def sync_data_transfers(dry_run: bool = False, *, db_session: Session | None = None):
    """Submit pending and poll in-progress Globus data transfers, notifying the
    workflow launcher (flips QueuedJob "staging" -> "pending"/"failed") once a
    run's input staging settles. Finalizing completed workflow runs once output
    transfers settle is owned solely by sync_completed_workflow_runs."""
    db_session = require_scheduler_db_session(db_session)
    logger.info("Checking for Globus data transfers to sync...")
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


def sync_workflow_repo_staging(dry_run: bool = False):
    """Submit pending and poll in-progress workflow repo stagings (GitHub repo
    checkouts cached on Gadi via S3 + Globus, see workflow_repo_staging.py),
    promoting any run still "staging" on a workflow whose repo just finished."""
    logger.info("Checking for workflow repo stagings to sync...")
    db_session = next(get_db())
    if dry_run:
        pending_count = db_session.scalar(
            select(func.count())
            .select_from(Workflow)
            .where(Workflow.repo_staging_status.in_(["pending", "in_progress"]))
        )
        logger.info(f"Dry run - found {pending_count} workflow repo staging(s) requiring sync.")
        return

    result = workflow_repo_staging.sync_workflow_repo_staging(db_session)
    logger.info(
        "Finished syncing workflow repo stagings: "
        f"checked={result.checked}, submitted={result.submitted}, "
        f"completed={result.completed}, failed={result.failed}, errored={result.errored}."
    )
