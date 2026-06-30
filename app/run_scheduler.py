from datetime import datetime, UTC

import typer
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from app.scheduler import SCHEDULER
from app.scheduler.jobs import submit_pending_jobs

SUBMIT_INTERVAL = IntervalTrigger(minutes=5)

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
        logger.info("Starting scheduler")
        SCHEDULER.start()
    finally:
        logger.info("Shutting down scheduler")
        if SCHEDULER.running:
            SCHEDULER.shutdown()


if __name__ == "__main__":
    typer.run(main)
