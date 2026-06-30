from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.memory import MemoryJobStore

from app.db import _get_database_url


JOB_STORES = {
    "memory": MemoryJobStore(),
    "db": SQLAlchemyJobStore(url=_get_database_url())
}
EXECUTORS = {
    "default": ThreadPoolExecutor(10)
}
JOB_DEFAULTS = {
    "coalesce": True,
    "misfire_grace_time": 5 * 60,
    "max_instances": 5,
}

SCHEDULER = BlockingScheduler(
    jobstores=JOB_STORES, executors=EXECUTORS, job_defaults=JOB_DEFAULTS
)
