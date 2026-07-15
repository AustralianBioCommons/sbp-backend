import asyncio
import os
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.job_queue import QueuedJob
from ..routes.dependencies import get_db
from ..schemas.workflows import WorkflowName
from ..services import health, seqera
from ..services.bindflow_executor import launch_bindflow_workflow
from ..services.proteinfold_executor import launch_proteinfold_workflow
from ..services.seqera import WorkflowLaunchResult
from ..services.seqera_errors import SeqeraAPIError, SeqeraConfigurationError
from ..services.wisps_executor import launch_wisps_workflow
from . import SCHEDULER

LAUNCH_MAX_ATTEMPTS = 3
RETRY_DELAY_BASE = 5 * 60

# Gadi's gpuhopper PBS queue holds 50 job slots and each workflow run occupies approximately 2 of
# them (Nextflow's queueSize), so 25 workflows can run concurrently. Hardcoded as a
# temporary MVP value, configurable via env var.
MAX_CONCURRENT_WORKFLOWS = int(os.getenv("MAX_CONCURRENT_WORKFLOWS", "25"))


class LaunchFunction(Protocol):
    """
    Type annotation for workflow launch functions
    """

    def __call__(
        self,
        *,
        queued_job: QueuedJob,
        dry_run: bool = False,
    ) -> Awaitable[WorkflowLaunchResult | None]: ...


def get_retry_delay(job: QueuedJob) -> timedelta:
    """
    Apply exponential backoff to the retry delay, based on number of attempts.
    """
    return timedelta(seconds=RETRY_DELAY_BASE * (2**job.attempts - 1))


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
    launch_func: LaunchFunction
    workflow_name: WorkflowName = cast(WorkflowName, job.workflow.name)
    if workflow_name in ("interaction-screening", "bulk-prediction"):
        launch_func = launch_wisps_workflow
    elif workflow_name in ("single-prediction", "proteinfold"):
        launch_func = launch_proteinfold_workflow
    elif workflow_name in ("de-novo-design", "bindflow", "bindcraft"):
        launch_func = launch_bindflow_workflow
    else:
        raise ValueError(f"Unsupported workflow: {job.workflow.name}")
    try:
        result = asyncio.run(launch_func(queued_job=job, dry_run=dry_run))
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


def get_available_workflow_capacity() -> int:
    """
    How many more workflows can be submitted to Gadi right now, per the Seqera API's
    count of workflows still occupying a job slot there (see MAX_CONCURRENT_WORKFLOWS).
    """
    active_workflow_count = asyncio.run(seqera.count_active_workflows())
    capacity = max(0, MAX_CONCURRENT_WORKFLOWS - active_workflow_count)
    logger.info(
        f"{active_workflow_count}/{MAX_CONCURRENT_WORKFLOWS} workflows active on Gadi "
        f"({capacity} submission slot(s) available)."
    )
    return capacity


def submit_pending_jobs(dry_run: bool = False):
    # Time between jobs
    job_offset = 10
    logger.info("Checking for pending jobs...")
    db_session = next(get_db())
    ok_to_launch = is_seqera_available(db_session)
    if not ok_to_launch:
        logger.warning("Skipping pending job submission while system status is unhealthy.")
        return

    try:
        available_capacity = get_available_workflow_capacity()
    except (SeqeraAPIError, SeqeraConfigurationError) as e:
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
    logger.info("TODO: refresh user credits - not implemented yet")
