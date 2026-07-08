from datetime import UTC, datetime

import pytest

from app import run_scheduler


class SchedulerStopped(Exception):
    pass


class RecordingScheduler:
    """
    Fake scheduler to check for expected calls.
    """
    def __init__(self, *, stop_on_start: bool = False):
        self.added_jobs = []
        self.events = []
        self.running = False
        self.stop_on_start = stop_on_start

    def add_job(self, func, **job_config):
        self.events.append(("add_job", job_config["id"]))
        self.added_jobs.append((func, job_config))

    def start(self):
        self.events.append(("start", None))
        self.running = True
        if self.stop_on_start:
            raise SchedulerStopped

    def shutdown(self):
        self.events.append(("shutdown", None))
        self.running = False


def test_main_adds_expected_jobs_and_starts_scheduler(monkeypatch):
    scheduler = RecordingScheduler()
    monkeypatch.setattr(run_scheduler, "SCHEDULER", scheduler)

    run_scheduler.main(dry_run=True)

    assert [job_config["id"] for _, job_config in scheduler.added_jobs] == [
        "submit_pending_jobs",
        "refresh_user_credits",
    ]

    submit_func, submit_config = scheduler.added_jobs[0]
    assert submit_func is run_scheduler.submit_pending_jobs
    assert submit_config["kwargs"] == {"dry_run": True}
    assert submit_config["jobstore"] == "memory"
    assert submit_config["trigger"] is run_scheduler.SUBMIT_INTERVAL

    refresh_func, refresh_config = scheduler.added_jobs[1]
    assert refresh_func is run_scheduler.refresh_user_credits
    assert refresh_config["jobstore"] == "db"
    assert refresh_config["trigger"] == run_scheduler.MONTHLY_TRIGGER

    assert scheduler.events == [
        ("add_job", "submit_pending_jobs"),
        ("add_job", "refresh_user_credits"),
        ("start", None),
        ("shutdown", None),
    ]


def test_main_shuts_down_scheduler_after_start_stops(monkeypatch):
    scheduler = RecordingScheduler(stop_on_start=True)
    monkeypatch.setattr(run_scheduler, "SCHEDULER", scheduler)

    with pytest.raises(SchedulerStopped):
        run_scheduler.main()

    assert scheduler.events[-2:] == [("start", None), ("shutdown", None)]
    assert scheduler.running is False
