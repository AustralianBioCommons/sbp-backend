from datetime import UTC, datetime

import typer
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.scheduler import SCHEDULER
from app.scheduler.jobs import refresh_user_credits, submit_pending_jobs

SUBMIT_INTERVAL = IntervalTrigger(minutes=5)
MONTHLY_TRIGGER = CronTrigger(day=1, hour=1, minute=0, timezone="UTC")


def main(dry_run: bool = False):
    try:
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
        if not SCHEDULER.get_job("refresh_user_credits", jobstore="db"):
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
                replace_existing=False,
                coalesce=True,
            )
        else:
            logger.info("Monthly refresh_user_credits already exists in scheduler")
        logger.info("Starting scheduler")
        SCHEDULER.start()
    finally:
        logger.info("Shutting down scheduler")
        if SCHEDULER.running:
            SCHEDULER.shutdown()


if __name__ == "__main__":
    typer.run(main)
