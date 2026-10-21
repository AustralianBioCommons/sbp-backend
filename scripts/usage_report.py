"""Monthly SBP usage report: users who logged in, bundle-approved active
users, and workflow execution counts by status, scoped to since the soft
launch date. Also writes a CSV with per-run details, the summary counts, and
the email lists for users with/without a bundle.

Run manually against whichever environment's DATABASE_URL is in .env:
    uv run python scripts/usage_report.py

Requires the target Postgres (e.g. local Docker container) to be up. The
since-launch user counts also need AUTH0_MGMT_DOMAIN/CLIENT_ID/CLIENT_SECRET
in .env (see auth0_management.py); without them they fall back to all-time
counts.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402
from auth0_management import Auth0ManagementSettings, fetch_last_login_by_user_id  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.db.models.core import AppUser, Workflow, WorkflowRun  # noqa: E402
from app.schemas.workflows.shared import map_pipeline_status_to_ui  # noqa: E402

UNKNOWN_WORKFLOW = "Unknown"

NOT_YET_SYNCED = "Not yet synced"

# Soft launch date - all counts are scoped to on/after this date so
# pre-launch test activity doesn't skew the numbers.
LAUNCH_DATE = datetime(2026, 10, 15, tzinfo=UTC)


@dataclass
class UsageReport:
    users_total: int
    # None when no Auth0 last_login lookup was supplied - caller should
    # report the *_total fields as all-time snapshots instead.
    users_since_launch: int | None
    active_users: int
    no_bundle_users_total: int
    no_bundle_users_since_launch: int | None
    run_status_counts: Counter[str]

    @property
    def total_runs(self) -> int:
        return sum(self.run_status_counts.values())

    @property
    def with_bundle_users_total(self) -> int:
        return self.users_total - self.no_bundle_users_total

    @property
    def with_bundle_users_since_launch(self) -> int | None:
        if self.users_since_launch is None or self.no_bundle_users_since_launch is None:
            return None
        return self.users_since_launch - self.no_bundle_users_since_launch


def compute_report(
    db: Session,
    since: datetime = LAUNCH_DATE,
    last_login_by_user_id: dict[str, datetime] | None = None,
) -> UsageReport:
    all_users = db.execute(
        select(AppUser.auth0_user_id, AppUser.sbp_bundle_credit_granted_at)
    ).all()
    no_bundle_ids = [auth0_id for auth0_id, bundle_at in all_users if bundle_at is None]

    def _since_launch_count(auth0_ids: list[str]) -> int | None:
        if last_login_by_user_id is None:
            return None
        return sum(
            1
            for auth0_id in auth0_ids
            if (last_login := last_login_by_user_id.get(auth0_id)) is not None
            and last_login >= since
        )

    has_run_since_launch = (
        select(WorkflowRun.id)
        .where(WorkflowRun.owner_user_id == AppUser.id, WorkflowRun.submission_timestamp >= since)
        .exists()
    )
    active_users = db.execute(
        select(func.count())
        .select_from(AppUser)
        .where(AppUser.sbp_bundle_credit_granted_at >= since, has_run_since_launch)
    ).scalar_one()

    statuses = (
        db.execute(
            select(WorkflowRun.seqera_final_status).where(WorkflowRun.submission_timestamp >= since)
        )
        .scalars()
        .all()
    )
    run_status_counts = Counter(
        map_pipeline_status_to_ui(status) if status else NOT_YET_SYNCED for status in statuses
    )

    return UsageReport(
        users_total=len(all_users),
        users_since_launch=_since_launch_count([auth0_id for auth0_id, _ in all_users]),
        active_users=active_users,
        no_bundle_users_total=len(no_bundle_ids),
        no_bundle_users_since_launch=_since_launch_count(no_bundle_ids),
        run_status_counts=run_status_counts,
    )


def fetch_workflow_runs(
    db: Session, since: datetime = LAUNCH_DATE
) -> list[tuple[str, datetime | None, str, str, str]]:
    """One row per workflow run since launch: (owner email, run time, status, workflow, tool)."""
    rows = db.execute(
        select(
            AppUser.email,
            WorkflowRun.submission_timestamp,
            WorkflowRun.seqera_final_status,
            Workflow.name,
            WorkflowRun.tool,
        )
        .join(AppUser, WorkflowRun.owner_user_id == AppUser.id)
        .join(Workflow, WorkflowRun.workflow_id == Workflow.id, isouter=True)
        .where(WorkflowRun.submission_timestamp >= since)
        .order_by(WorkflowRun.submission_timestamp)
    ).all()
    return [
        (
            email,
            run_at,
            map_pipeline_status_to_ui(status) if status else NOT_YET_SYNCED,
            workflow_name or UNKNOWN_WORKFLOW,
            tool or UNKNOWN_WORKFLOW,
        )
        for email, run_at, status, workflow_name, tool in rows
    ]


def fetch_user_emails_by_bundle(db: Session) -> tuple[list[str], list[str]]:
    """(emails with a bundle, emails without a bundle), each sorted."""
    rows = db.execute(select(AppUser.email, AppUser.sbp_bundle_credit_granted_at)).all()
    with_bundle = sorted(email for email, bundle_at in rows if bundle_at is not None)
    no_bundle = sorted(email for email, bundle_at in rows if bundle_at is None)
    return with_bundle, no_bundle


def write_csv(
    path: Path,
    report: UsageReport,
    runs: list[tuple[str, datetime | None, str, str, str]],
    with_bundle_emails: list[str],
    no_bundle_emails: list[str],
    since: datetime = LAUNCH_DATE,
) -> None:
    unavailable = "n/a (set AUTH0_MGMT_* in .env)"

    def _or_unavailable(value: int | None) -> int | str:
        return unavailable if value is None else value

    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "run_at", "status", "workflow", "tool"])
        for email, run_at, status, workflow_name, tool in runs:
            writer.writerow(
                [email, run_at.isoformat() if run_at else "", status, workflow_name, tool]
            )

        writer.writerow([])
        writer.writerow(["metric", f"since {since.date().isoformat()}", "all-time"])
        writer.writerow(
            [
                "users logged in, with bundle",
                _or_unavailable(report.with_bundle_users_since_launch),
                report.with_bundle_users_total,
            ]
        )
        writer.writerow(
            [
                "users logged in, without bundle",
                _or_unavailable(report.no_bundle_users_since_launch),
                report.no_bundle_users_total,
            ]
        )
        writer.writerow(["active users (bundle + ran >=1 workflow)", report.active_users, ""])
        writer.writerow(["workflows executed", report.total_runs, ""])
        for label, count in sorted(report.run_status_counts.items(), key=lambda kv: -kv[1]):
            writer.writerow([f"  workflow status: {label}", count, ""])

        writer.writerow([])
        writer.writerow([f"users with bundle ({len(with_bundle_emails)})"])
        for email in with_bundle_emails:
            writer.writerow([email])

        writer.writerow([])
        writer.writerow([f"users without bundle ({len(no_bundle_emails)})"])
        for email in no_bundle_emails:
            writer.writerow([email])


def print_report(report: UsageReport, since: datetime = LAUNCH_DATE) -> None:
    since_label = since.date().isoformat()
    print(f"SBP usage report (since {since_label})")
    print("=================")
    if report.users_since_launch is None:
        print(
            f"Users logged in: {report.users_total} [all-time - set AUTH0_MGMT_* in .env "
            "for a since-launch estimate]"
        )
        print(f"  of which, no bundle: {report.no_bundle_users_total} [all-time]")
    else:
        print(
            "Users logged in since launch (Auth0 last_login, tenant-wide approximation): "
            f"{report.users_since_launch} (all-time: {report.users_total})"
        )
        print(
            f"  of which, no bundle, since launch: {report.no_bundle_users_since_launch} "
            f"(all-time: {report.no_bundle_users_total})"
        )
    print(
        f"  of which, active (bundle approved + ran >=1 workflow, since launch): {report.active_users}"
    )
    print()
    print(f"Workflows executed since launch: {report.total_runs}")
    for label, count in sorted(report.run_status_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")


def main() -> None:
    auth0_settings = Auth0ManagementSettings()
    try:
        with SessionLocal() as db:
            last_login_by_user_id = None
            if auth0_settings.is_configured():
                auth0_user_ids = db.execute(select(AppUser.auth0_user_id)).scalars().all()
                last_login_by_user_id = fetch_last_login_by_user_id(auth0_settings, auth0_user_ids)
            report = compute_report(db, last_login_by_user_id=last_login_by_user_id)
            runs = fetch_workflow_runs(db)
            with_bundle_emails, no_bundle_emails = fetch_user_emails_by_bundle(db)
    except OperationalError:
        print(
            "Could not connect to the database - check DATABASE_URL in .env and make sure "
            "Postgres (e.g. your local Docker container) is running.",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as exc:
        print(
            f"Could not reach Auth0's Management API ({exc}) - check AUTH0_MGMT_DOMAIN/"
            "CLIENT_ID/CLIENT_SECRET in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    print_report(report)

    csv_path = Path(f"usage_report_{datetime.now(UTC).date().isoformat()}.csv")
    write_csv(csv_path, report, runs, with_bundle_emails, no_bundle_emails)
    print(f"\nSaved detailed CSV (per-run details, summary, and user email lists) to {csv_path}")


if __name__ == "__main__":
    main()
