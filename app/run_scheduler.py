from datetime import UTC, datetime, timedelta

import typer
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger

# Load .env before importing scheduler modules, which read required env vars (e.g.
# SEQERA_API_URL) at call time. Unlike app/main.py, this entrypoint runs standalone
# (`python -m app.run_scheduler`) and never imports main.py, so without this call
# .env values are never loaded into the process environment.
load_dotenv()

from app.scheduler import SCHEDULER  # noqa: E402
from app.scheduler.jobs import (  # noqa: E402
    refresh_seqera_health_status,
    refresh_user_credits,
    submit_pending_jobs,
    sync_completed_workflow_runs,
    sync_data_transfers,
    sync_workflow_repo_staging,
)

SUBMIT_INTERVAL = IntervalTrigger(minutes=5)
SYNC_INTERVAL = IntervalTrigger(minutes=10)
DATA_TRANSFER_SYNC_INTERVAL = IntervalTrigger(minutes=2)
REPO_STAGING_SYNC_INTERVAL = IntervalTrigger(minutes=2)
# Sole source of live Seqera health probes now - submit_pending_jobs/launch_job/
# sync_completed_workflow_runs only read the cache this job refreshes (see
# is_seqera_available). Matched against SEQERA_HEALTH_CACHE_TTL_SECONDS so the
# cache stays fresh for other readers (admin dashboard, portal banner) too.
HEALTH_CHECK_INTERVAL = IntervalTrigger(minutes=2)
# Fixed AEST (UTC+10), no DST. Not Australia/Sydney: APScheduler 3.11.3's CronTrigger
# miscalculates day=1 across Sydney's October DST switch and skips November entirely.
MONTHLY_TRIGGER = CronTrigger(day=1, hour=0, minute=0, timezone="Australia/Brisbane")


def main(dry_run: bool = False):
    try:
        logger.info(
            f"Adding refresh_seqera_health_status to scheduler: trigger = {HEALTH_CHECK_INTERVAL}"
        )
        SCHEDULER.add_job(
            refresh_seqera_health_status,
            kwargs={"dry_run": dry_run},
            jobstore="memory",
            trigger=HEALTH_CHECK_INTERVAL,
            next_run_time=datetime.now(tz=UTC),
            id="refresh_seqera_health_status",
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(f"Adding submit_pending_jobs to scheduler: trigger = {SUBMIT_INTERVAL}")
        SCHEDULER.add_job(
            submit_pending_jobs,
            kwargs={"dry_run": dry_run},
            jobstore="memory",
            trigger=SUBMIT_INTERVAL,
            next_run_time=datetime.now(tz=UTC),
            id="submit_pending_jobs",
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(f"Adding sync_completed_workflow_runs to scheduler: trigger = {SYNC_INTERVAL}")
        SCHEDULER.add_job(
            sync_completed_workflow_runs,
            kwargs={"dry_run": dry_run},
            jobstore="memory",
            trigger=SYNC_INTERVAL,
            next_run_time=datetime.now(tz=UTC) + timedelta(minutes=2),
            id="sync_completed_workflow_runs",
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            f"Adding sync_data_transfers to scheduler: trigger = {DATA_TRANSFER_SYNC_INTERVAL}"
        )
        SCHEDULER.add_job(
            sync_data_transfers,
            kwargs={"dry_run": dry_run},
            jobstore="memory",
            trigger=DATA_TRANSFER_SYNC_INTERVAL,
            next_run_time=datetime.now(tz=UTC) + timedelta(minutes=1),
            id="sync_data_transfers",
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            f"Adding sync_workflow_repo_staging to scheduler: trigger = {REPO_STAGING_SYNC_INTERVAL}"
        )
        SCHEDULER.add_job(
            sync_workflow_repo_staging,
            kwargs={"dry_run": dry_run},
            jobstore="memory",
            trigger=REPO_STAGING_SYNC_INTERVAL,
            next_run_time=datetime.now(tz=UTC) + timedelta(minutes=1),
            id="sync_workflow_repo_staging",
            misfire_grace_time=60,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            f"Adding monthly refresh_user_credits to scheduler: trigger = {MONTHLY_TRIGGER}"
        )
        logger.info(
            f"Next refresh at: {MONTHLY_TRIGGER.get_next_fire_time(previous_fire_time=None, now=datetime.now(tz=UTC))}"
        )
        SCHEDULER.add_job(
            refresh_user_credits,
            kwargs={"dry_run": dry_run},
            jobstore="db",
            trigger=MONTHLY_TRIGGER,
            id="refresh_user_credits",
            misfire_grace_time=60 * 60 * 24,
            max_instances=1,
            replace_existing=True,
            coalesce=True,
        )
        logger.info("Starting scheduler")
        SCHEDULER.start()
    finally:
        logger.info("Shutting down scheduler")
        if SCHEDULER.running:
            SCHEDULER.shutdown()


if __name__ == "__main__":
    typer.run(main)
