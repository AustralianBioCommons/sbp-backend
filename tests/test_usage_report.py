"""Self-check for scripts/usage_report.py's counting logic."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.db.models.core import AppUser, Workflow, WorkflowRun

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from usage_report import (  # noqa: E402
    compute_report,
    fetch_user_emails_by_bundle,
    fetch_workflow_runs,
    write_csv,
)

SINCE = datetime(2026, 10, 15, tzinfo=UTC)
BEFORE = SINCE - timedelta(days=5)
AFTER = SINCE + timedelta(days=1)


def _make_user(db, *, bundle_granted_at: datetime | None) -> AppUser:
    unique = uuid4().hex
    user = AppUser(
        auth0_user_id=f"auth0|{unique}",
        name="test",
        email=f"{unique}@example.com",
        sbp_bundle_credit_granted_at=bundle_granted_at,
    )
    db.add(user)
    db.flush()
    return user


def _make_run(
    db,
    owner: AppUser,
    *,
    status: str | None,
    submitted_at: datetime,
    workflow: Workflow | None = None,
    tool: str | None = None,
) -> WorkflowRun:
    run = WorkflowRun(
        owner_user_id=owner.id,
        workflow=workflow,
        tool=tool,
        work_dir="/work",
        seqera_final_status=status,
        submission_timestamp=submitted_at,
    )
    db.add(run)
    db.flush()
    return run


def _make_workflow(db, *, name: str) -> Workflow:
    workflow = Workflow(
        name=name,
        repo_url="https://example.com/repo.git",
        default_revision="main",
        config_path="config.yaml",
    )
    db.add(workflow)
    db.flush()
    return workflow


def test_compute_report_without_auth0_lookup_falls_back_to_all_time(test_db):
    active_since_launch = _make_user(test_db, bundle_granted_at=AFTER)
    _make_run(test_db, active_since_launch, status="SUCCEEDED", submitted_at=AFTER)

    pre_launch_tester = _make_user(test_db, bundle_granted_at=BEFORE)
    _make_run(test_db, pre_launch_tester, status="SUCCEEDED", submitted_at=AFTER)

    no_bundle_idle = _make_user(test_db, bundle_granted_at=None)
    test_db.commit()

    report = compute_report(test_db, since=SINCE)

    assert report.users_total == 3
    assert report.users_since_launch is None  # no Auth0 lookup supplied
    assert report.no_bundle_users_total == 1
    assert report.no_bundle_users_since_launch is None
    assert report.active_users == 1
    # Workflow-execution counts don't need Auth0 - both runs were submitted after SINCE.
    assert report.total_runs == 2
    assert report.run_status_counts == {"Completed": 2}
    assert no_bundle_idle.id is not None  # fixture sanity: row was created


def test_compute_report_scopes_users_by_auth0_last_login(test_db):
    active_since_launch = _make_user(test_db, bundle_granted_at=AFTER)
    _make_run(test_db, active_since_launch, status="SUCCEEDED", submitted_at=AFTER)

    # Bundle approved after launch but their only run predates it -> not
    # counted as active (no run *since launch*), even though they did log in since launch.
    bundled_only_old_run = _make_user(test_db, bundle_granted_at=AFTER)
    _make_run(test_db, bundled_only_old_run, status="SUCCEEDED", submitted_at=BEFORE)

    no_bundle_recent_login = _make_user(test_db, bundle_granted_at=None)
    no_bundle_old_login = _make_user(test_db, bundle_granted_at=None)
    unknown_to_auth0 = _make_user(test_db, bundle_granted_at=None)  # e.g. lookup failed/deleted
    test_db.commit()

    last_login_by_user_id = {
        active_since_launch.auth0_user_id: AFTER,
        bundled_only_old_run.auth0_user_id: AFTER,
        no_bundle_recent_login.auth0_user_id: AFTER,
        no_bundle_old_login.auth0_user_id: BEFORE,
    }

    report = compute_report(test_db, since=SINCE, last_login_by_user_id=last_login_by_user_id)

    assert report.users_total == 5
    assert (
        report.users_since_launch == 3
    )  # active_since_launch, bundled_only_old_run, no_bundle_recent_login
    assert report.active_users == 1
    assert report.no_bundle_users_total == 3
    assert report.no_bundle_users_since_launch == 1  # only no_bundle_recent_login
    assert report.with_bundle_users_total == 2
    assert report.with_bundle_users_since_launch == 2  # active_since_launch, bundled_only_old_run
    assert unknown_to_auth0.id is not None  # fixture sanity: row was created


def test_fetch_workflow_runs_joins_email_workflow_tool_and_maps_status_in_order(test_db):
    single_pred = _make_workflow(test_db, name="single-prediction")
    alice = _make_user(test_db, bundle_granted_at=AFTER)
    bob = _make_user(test_db, bundle_granted_at=None)
    _make_run(
        test_db,
        bob,
        status="FAILED",
        submitted_at=AFTER + timedelta(hours=1),
        workflow=None,  # no linked Workflow row -> falls back to "Unknown"
        tool="colabfold",
    )
    _make_run(
        test_db,
        alice,
        status="SUCCEEDED",
        submitted_at=AFTER,
        workflow=single_pred,
        tool="alphafold2",
    )
    _make_run(test_db, alice, status=None, submitted_at=BEFORE)  # excluded, pre-launch
    test_db.commit()

    runs = fetch_workflow_runs(test_db, since=SINCE)

    # SQLite (unlike Postgres) drops tzinfo on round-trip, so compare naive.
    naive_runs = [
        (email, run_at.replace(tzinfo=None), status, workflow_name, tool)
        for email, run_at, status, workflow_name, tool in runs
    ]
    assert naive_runs == [
        (alice.email, AFTER.replace(tzinfo=None), "Completed", "single-prediction", "alphafold2"),
        (
            bob.email,
            (AFTER + timedelta(hours=1)).replace(tzinfo=None),
            "Failed",
            "Unknown",
            "colabfold",
        ),
    ]


def test_fetch_user_emails_by_bundle_splits_and_sorts(test_db):
    zed_with = _make_user(test_db, bundle_granted_at=AFTER)
    amy_with = _make_user(test_db, bundle_granted_at=BEFORE)
    zed_without = _make_user(test_db, bundle_granted_at=None)
    amy_without = _make_user(test_db, bundle_granted_at=None)
    test_db.commit()

    with_bundle, no_bundle = fetch_user_emails_by_bundle(test_db)

    assert with_bundle == sorted([zed_with.email, amy_with.email])
    assert no_bundle == sorted([zed_without.email, amy_without.email])


def test_write_csv_includes_run_rows_with_workflow_tool_summary_and_email_lists(test_db, tmp_path):
    single_pred = _make_workflow(test_db, name="single-prediction")
    alice = _make_user(test_db, bundle_granted_at=AFTER)
    _make_run(
        test_db,
        alice,
        status="SUCCEEDED",
        submitted_at=AFTER,
        workflow=single_pred,
        tool="alphafold2",
    )
    bob = _make_user(test_db, bundle_granted_at=None)
    test_db.commit()

    report = compute_report(test_db, since=SINCE)
    runs = fetch_workflow_runs(test_db, since=SINCE)
    with_bundle_emails, no_bundle_emails = fetch_user_emails_by_bundle(test_db)
    csv_path = tmp_path / "report.csv"

    write_csv(csv_path, report, runs, with_bundle_emails, no_bundle_emails, since=SINCE)
    content = csv_path.read_text()

    assert "email,run_at,status,workflow,tool" in content
    # SQLite (unlike Postgres) drops tzinfo on round-trip, so the written
    # timestamp is naive here even though AFTER is tz-aware.
    assert (
        f"{alice.email},{AFTER.replace(tzinfo=None).isoformat()},Completed,single-prediction,alphafold2"
        in content
    )
    assert "users logged in, with bundle" in content
    assert "users logged in, without bundle" in content
    assert "n/a (set AUTH0_MGMT_* in .env)" in content  # no Auth0 lookup supplied in this test
    assert "users with bundle (1)" in content
    assert alice.email in content
    assert "users without bundle (1)" in content
    assert bob.email in content
