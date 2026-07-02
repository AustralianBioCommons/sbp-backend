import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.job_queue import QueuedJob
from ..routes.dependencies import get_db
from ..services import health
from ..services.wisps_executor import launch_wisps_workflow_new
from . import SCHEDULER

LAUNCH_MAX_ATTEMPTS = 3
RETRY_DELAY_BASE = 5 * 60


def get_retry_delay(job: QueuedJob) -> timedelta:
    """
    Apply exponential backoff to the retry delay, based on number of attempts.
    """
    return timedelta(seconds=RETRY_DELAY_BASE * (2 ** job.attempts - 1))


def is_seqera_available(db_session: Session) -> bool:
    system_status = asyncio.run(health.get_system_status(db_session))
    logger.info(f"System status is {system_status.overall_status}.")
    return system_status.overall_status == "healthy"


def launch_job(job_id: UUID, dry_run: bool = False) -> None:
    logger.info(f"Launching job {job_id}...")
    db_session = next(get_db())

    ok_to_launch = is_seqera_available(db_session)
    if not ok_to_launch:
        logger.warning("Skipping job launching while system status is unhealthy.")
        return
    job = db_session.get(QueuedJob, job_id)
    if job is None:
        return

    now = datetime.now(tz=UTC)
    launch_func: Callable
    if job.workflow.name == "interaction-screening":
        launch_func = launch_wisps_workflow_new
    # TODO: add launch functions for other workflows
    else:
        raise ValueError(f"Unsupported workflow: {job.workflow.name}")
    try:
        asyncio.run(launch_func(queued_job=job, dry_run=dry_run))
        if dry_run:
            logger.info("Dry run - not updating job status")
        else:
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
            job.error = str(e)
            job.last_attempt_at = now
            if job.attempts >= LAUNCH_MAX_ATTEMPTS:
                job.status = "failed"
            else:
                job.status = "pending"
                delay = get_retry_delay(job)
                job.next_attempt_at = now + delay
            db_session.add(job)
            db_session.commit()
        return


def submit_pending_jobs(dry_run: bool = False):
    logger.info("Checking for pending jobs...")
    db_session = next(get_db())
    ok_to_launch = is_seqera_available(db_session)
    if not ok_to_launch:
        logger.warning("Skipping pending job submission while system status is unhealthy.")
        return

    now = datetime.now(tz=UTC)

    pending_query = select(QueuedJob).where(
        QueuedJob.status == "pending",
        QueuedJob.next_attempt_at <= now
    )

    pending_jobs = db_session.scalars(pending_query).all()
    logger.info(f"Found {len(pending_jobs)} pending jobs.")
    for job in pending_jobs:
        launch_id = f"launch_job_{job.id}"
        # Ignore if already scheduled
        if SCHEDULER.get_job(launch_id, jobstore="memory") is not None:
            continue

        SCHEDULER.add_job(
            launch_job,
            jobstore="memory",
            kwargs={"job_id": job.id, "dry_run": dry_run},
            name=launch_id,
            max_instances=1,
            replace_existing=True,
        )

    logger.info("Finished submitting pending jobs.")
