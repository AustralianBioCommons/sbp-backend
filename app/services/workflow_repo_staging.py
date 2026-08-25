"""Stage a workflow's GitHub repo onto Gadi via S3 + Globus, cached by
(revision, commit) pair.

Gadi compute nodes have no network access, so Nextflow can't fetch pipeline
code from GitHub itself - the repo must already be on Gadi's filesystem, the
same way input files are staged there via Globus. A repo checkout is shared
across every run of a workflow, so it's cached on the Workflow row instead of
per run: if default_revision still resolves to the commit already
staged/staging under that same revision, launches reuse it for free.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import globus_sdk
from github import Auth, Github, GithubException, GithubIntegration
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import GlobusSettings, Settings, get_settings
from ..db.models.core import Workflow
from .globus_client import get_transfer_client
from .globus_errors import GlobusTransferError
from .globus_transfer import _gadi_relative_path
from .s3 import get_s3_client

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 120


class RepoStagingError(RuntimeError):
    """Raised when resolving or staging a workflow's GitHub repo fails."""


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a ``https://github.com/<owner>/<repo>`` URL."""
    parsed = urlparse(repo_url)
    if parsed.netloc != "github.com":
        raise RepoStagingError(f"Only github.com repo URLs are supported, got: {repo_url}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise RepoStagingError(f"Could not parse owner/repo from repo_url: {repo_url}")
    owner, repo = parts[0], parts[1]
    return owner, repo.removesuffix(".git")


def _get_github_client(owner: str, repo: str, *, settings: Settings) -> Github:
    """Build a PyGithub client authorized to read owner/repo.

    Prefers GitHub App installation auth.
    get_repo_installation looks up which installation of the App covers this
    repo, and get_github_for_installation returns a client whose short-lived
    token PyGithub mints and refreshes automatically. Falls back to a plain
    token, then to unauthenticated, for local development only.
    """
    github_settings = settings.github
    if (
        github_settings.workflow_staging_automation_app_id
        and github_settings.workflow_staging_automation_app_private_key
    ):
        integration = GithubIntegration(
            auth=Auth.AppAuth(
                github_settings.workflow_staging_automation_app_id,
                github_settings.workflow_staging_automation_app_private_key,
            )
        )
        installation = integration.get_repo_installation(owner, repo)
        return integration.get_github_for_installation(installation.id)
    if github_settings.workflow_staging_automation_token:
        return Github(auth=Auth.Token(github_settings.workflow_staging_automation_token))
    return Github()


def resolve_latest_commit_sha(
    repo_url: str, revision: str, *, settings: Settings | None = None
) -> str:
    """Resolve a branch/tag/ref to its current commit sha via the GitHub API.

    One lightweight network call - no local clone needed - cheap enough to
    run on every launch to check whether the staged copy is still current.
    """
    settings = settings or get_settings()
    owner, repo = parse_github_repo(repo_url)
    client = _get_github_client(owner, repo, settings=settings)
    try:
        gh_repo = client.get_repo(f"{owner}/{repo}")
        commit = gh_repo.get_commit(revision)
    except GithubException as exc:
        raise RepoStagingError(
            f"Failed to resolve commit for {owner}/{repo}@{revision}: {exc}"
        ) from exc
    if not commit.sha:
        raise RepoStagingError(f"GitHub API returned no commit sha for {owner}/{repo}@{revision}")
    return cast(str, commit.sha)


def build_repo_s3_prefix(owner: str, repo: str, revision: str, commit_sha: str) -> str:
    return f"workflow-repos/{owner}-{repo}/{revision}/{commit_sha}"


def build_repo_assets_s3_prefix(owner: str, repo: str, revision: str, commit_sha: str) -> str:
    return f"workflow-repos-assets/{owner}-{repo}/{revision}/{commit_sha}"


def build_repo_gadi_path(
    owner: str, repo: str, revision: str, commit_sha: str, *, globus_settings: GlobusSettings
) -> str:
    # revision is part of the path (not just commit_sha) because the bare repo
    # only gets a ref named after `revision` (see stage_pending_repo's
    # refspec) - two different revisions can resolve to the same commit_sha
    # (e.g. a freshly-cut branch), and without revision in the path they'd
    # collide on one bare repo that only has a ref for whichever was staged
    # first, breaking checkout of the other's name.
    #
    # ".git" suffix required: Seqera's launch API rejects a `pipeline`
    # "file:<path>" URL without it, and Nextflow then resolves that path as a
    # git-dir directly (`--git-dir=<path>`) - so this must be a real bare
    # repo, not a checkout with a nested .git/ one level down.
    return (
        f"{globus_settings.gadi_collection_root}/workflow_repos/"
        f"{owner}-{repo}/{revision}/{commit_sha}.git"
    )


def build_repo_assets_gadi_path(
    owner: str, repo: str, revision: str, commit_sha: str, *, globus_settings: GlobusSettings
) -> str:
    """Path to a real (non-bare) clone of the same commit, for reading
    pipeline-bundled assets (e.g. bindcraft's default settings JSON) as plain
    files - the bare repo build_repo_gadi_path points at has no working-tree
    files of its own.

    Deliberately at ``$NXF_ASSETS/local/<commit_sha>`` (get_executor_script
    points NXF_ASSETS at build_repo_gadi_path's parent) - that's exactly where
    Nextflow itself checks out a `file:` bare-repo `pipeline` when it doesn't
    find one already there. Pre-staging a real clone (see
    _clone_working_checkout) at that path means Nextflow finds its own
    checkout already done and skips redoing it; staging anything less than a
    real git checkout there (e.g. a `git archive` extract with no `.git/`)
    makes Nextflow fail with "Repository may be corrupted" instead.

    revision is part of the path for the same reason as build_repo_gadi_path:
    two different revisions can resolve to the same commit_sha, and without
    revision here they'd collide on one local checkout directory.
    """
    return (
        f"{globus_settings.gadi_collection_root}/workflow_repos/"
        f"{owner}-{repo}/{revision}/local/{commit_sha}"
    )


@dataclass(frozen=True)
class RepoStagingLocations:
    """Where a workflow's staged repo lives on Gadi: the bare repo for
    Seqera's `pipeline` field, and a real checkout at Nextflow's own
    NXF_ASSETS local-checkout slot for pipeline-bundled assets (see
    build_repo_assets_gadi_path)."""

    gadi_path: str
    assets_gadi_path: str


def ensure_repo_staging_requested(
    db: Session, workflow: Workflow, *, settings: Settings | None = None
) -> RepoStagingLocations:
    """Resolve the workflow's current commit, returning the Gadi paths it
    will live at, and kick off staging if that commit isn't already
    staged/staging.

    Called synchronously at launch time - only the cheap GitHub API call
    happens here. The actual clone/upload/Globus transfer happens later via
    sync_workflow_repo_staging, so launch requests stay fast.
    """
    settings = settings or get_settings()
    owner, repo = parse_github_repo(workflow.repo_url)
    revision = workflow.default_revision
    commit_sha = resolve_latest_commit_sha(workflow.repo_url, revision, settings=settings)
    gadi_path = build_repo_gadi_path(
        owner, repo, revision, commit_sha, globus_settings=settings.globus
    )
    assets_gadi_path = build_repo_assets_gadi_path(
        owner, repo, revision, commit_sha, globus_settings=settings.globus
    )

    # Comparing the full gadi_path (not just commit_sha) means a revision
    # change is always detected, even when the new revision happens to
    # resolve to the same commit_sha as before - gadi_path bakes revision in,
    # so a stale value here means either the commit or the revision moved.
    up_to_date = workflow.repo_gadi_path == gadi_path and workflow.repo_staging_status in (
        "pending",
        "in_progress",
        "completed",
    )
    if not up_to_date:
        workflow.repo_staged_commit_sha = commit_sha
        workflow.repo_staging_status = "pending"
        workflow.repo_gadi_path = gadi_path
        workflow.repo_staging_transfer_id = None
        workflow.repo_staging_error_message = None
        workflow.repo_staging_updated_at = datetime.now(UTC)
        db.add(workflow)
        db.commit()

    return RepoStagingLocations(gadi_path=gadi_path, assets_gadi_path=assets_gadi_path)


def _run_git(*args: str, cwd: str) -> None:
    """Run a git command, raising RepoStagingError with its stderr on failure."""
    result = subprocess.run(  # noqa: S603 - fixed "git" executable, args are list-form (no shell)
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_DOWNLOAD_TIMEOUT,
    )
    if result.returncode != 0:
        raise RepoStagingError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _clone_working_checkout(bare_dir: str, dest_dir: str) -> None:
    """Clone a real (non-bare) working checkout from the already-fetched bare
    repo - a local filesystem clone, not a second network fetch, checking out
    whatever the bare repo's HEAD points at (the branch set up in
    _clone_and_upload_repo). Unlike a `git archive` extract, this produces a
    real `.git/` directory, so what lands at build_repo_assets_gadi_path is
    indistinguishable from a checkout Nextflow would have produced itself
    there - see that function for why that matters."""
    _run_git("clone", bare_dir, dest_dir, cwd=bare_dir)


def _clone_and_upload_repo(
    owner: str,
    repo: str,
    commit_sha: str,
    repo_url: str,
    s3_prefix: str,
    *,
    revision: str,
    settings: Settings,
) -> None:
    """Fetch commit_sha into a bare repo and upload every file to S3 under
    s3_prefix (bare repo contents land directly at s3_prefix's root, not
    nested under .git/), plus a real working checkout of the same commit
    under build_repo_assets_s3_prefix for pipeline-bundled asset files.

    Fetches into a branch named after `revision` rather than leaving the
    commit as bare FETCH_HEAD, since a shallow-fetched bare repo has no other
    branches and the `revision` sent to Seqera must reference a real ref.
    """
    s3_client = get_s3_client(settings)
    bucket = settings.aws.s3_bucket
    refspec = f"{commit_sha}:refs/heads/{revision}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        logger.info("Cloning %s/%s@%s (bare) into %s", owner, repo, commit_sha, tmp_dir)
        try:
            _run_git("init", "--bare", cwd=tmp_dir)
            _run_git("remote", "add", "origin", repo_url, cwd=tmp_dir)
            _run_git("fetch", "--depth", "1", "origin", refspec, cwd=tmp_dir)
            _run_git("symbolic-ref", "HEAD", f"refs/heads/{revision}", cwd=tmp_dir)
            # A shallow fetch stores objects loose (one file per blob/tree) -
            # `gc` packs them into one file, since a real pipeline's tree can
            # be hundreds of small files otherwise. gc.packRefs=false keeps
            # the branch ref itself as a loose file: we only upload actual
            # files (no empty-directory markers), so a ref packed away would
            # leave refs/heads with nothing to upload and never reappear on
            # Gadi.
            _run_git("-c", "gc.packRefs=false", "gc", cwd=tmp_dir)
        except RepoStagingError as exc:
            raise RepoStagingError(f"Failed to clone {owner}/{repo}@{commit_sha}: {exc}") from exc

        tmp_path = Path(tmp_dir)
        uploaded = 0
        for file_path in tmp_path.rglob("*"):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(tmp_path).as_posix()
            s3_client.upload_file(str(file_path), bucket, f"{s3_prefix}/{relative_path}")
            uploaded += 1
        logger.info(
            "Uploaded %d file(s) for bare repo %s/%s@%s to s3://%s/%s",
            uploaded,
            owner,
            repo,
            commit_sha,
            bucket,
            s3_prefix,
        )

        assets_s3_prefix = build_repo_assets_s3_prefix(owner, repo, revision, commit_sha)
        with tempfile.TemporaryDirectory() as assets_tmp_dir:
            try:
                _clone_working_checkout(tmp_dir, assets_tmp_dir)
            except RepoStagingError as exc:
                raise RepoStagingError(
                    f"Failed to clone working checkout for {owner}/{repo}@{commit_sha}: {exc}"
                ) from exc

            assets_path = Path(assets_tmp_dir)
            assets_uploaded = 0
            for file_path in assets_path.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(assets_path).as_posix()
                s3_client.upload_file(str(file_path), bucket, f"{assets_s3_prefix}/{relative_path}")
                assets_uploaded += 1
            logger.info(
                "Uploaded %d working-checkout file(s) for %s/%s@%s to s3://%s/%s",
                assets_uploaded,
                owner,
                repo,
                commit_sha,
                bucket,
                assets_s3_prefix,
            )


def stage_pending_repo(
    db: Session, workflow: Workflow, *, settings: Settings | None = None
) -> None:
    """Submit the S3 upload + Globus transfer for a "pending" workflow repo."""
    settings = settings or get_settings()
    owner, repo = parse_github_repo(workflow.repo_url)
    revision = workflow.default_revision
    commit_sha = workflow.repo_staged_commit_sha
    if not commit_sha:
        raise RepoStagingError(f"Workflow {workflow.id} has no commit sha to stage")

    s3_prefix = build_repo_s3_prefix(owner, repo, revision, commit_sha)
    gadi_path = workflow.repo_gadi_path or build_repo_gadi_path(
        owner, repo, revision, commit_sha, globus_settings=settings.globus
    )
    assets_s3_prefix = build_repo_assets_s3_prefix(owner, repo, revision, commit_sha)
    assets_gadi_path = build_repo_assets_gadi_path(
        owner, repo, revision, commit_sha, globus_settings=settings.globus
    )

    try:
        _clone_and_upload_repo(
            owner,
            repo,
            commit_sha,
            workflow.repo_url,
            s3_prefix,
            revision=workflow.default_revision,
            settings=settings,
        )

        transfer_client = get_transfer_client(settings.globus)
        # Persist the submission id before submit_transfer, same as
        # DataTransfer.transfer_id, so a crash-and-retry doesn't double-submit.
        submission_id = workflow.repo_staging_transfer_id
        if not submission_id:
            submission_id = cast(str, transfer_client.get_submission_id()["value"])
            workflow.repo_staging_transfer_id = submission_id
            db.add(workflow)
            db.commit()

        transfer_data = globus_sdk.TransferData(
            settings.globus.s3_collection_id,
            settings.globus.gadi_collection_id,
            submission_id=submission_id,
            label=f"sbp-repo-{owner}-{repo}-{commit_sha[:12]}",
        )
        # gadi_path is absolute, but the Gadi collection's root maps to
        # GLOBUS_GADI_COLLECTION_ROOT, not "/" - convert before add_item.
        destination_path = _gadi_relative_path(gadi_path, globus_settings=settings.globus)
        transfer_data.add_item(f"/{s3_prefix}", destination_path, recursive=True)
        # Second item: the working checkout, landing at Nextflow's own
        # NXF_ASSETS local-checkout slot (see build_repo_assets_gadi_path).
        assets_destination_path = _gadi_relative_path(
            assets_gadi_path, globus_settings=settings.globus
        )
        transfer_data.add_item(f"/{assets_s3_prefix}", assets_destination_path, recursive=True)
        result = transfer_client.submit_transfer(transfer_data)
    except (RepoStagingError, globus_sdk.GlobusAPIError) as exc:
        logger.warning("Workflow repo staging failed for %s: %s", workflow.id, exc)
        workflow.repo_staging_status = "failed"
        workflow.repo_staging_transfer_id = None
        workflow.repo_staging_error_message = str(exc)
        workflow.repo_staging_updated_at = datetime.now(UTC)
        db.add(workflow)
        db.commit()
        return

    workflow.repo_gadi_path = gadi_path
    workflow.repo_staging_transfer_id = result["task_id"]
    workflow.repo_staging_status = "in_progress"
    workflow.repo_staging_updated_at = datetime.now(UTC)
    db.add(workflow)
    db.commit()


def poll_repo_staging(db: Session, workflow: Workflow, *, settings: Settings | None = None) -> None:
    """Poll Globus for an "in_progress" workflow repo transfer's task status."""
    if not workflow.repo_staging_transfer_id:
        raise GlobusTransferError(f"Workflow {workflow.id} has no transfer_id to poll")

    settings = settings or get_settings()
    transfer_client = get_transfer_client(settings.globus)
    try:
        task = transfer_client.get_task(workflow.repo_staging_transfer_id)
    except globus_sdk.GlobusAPIError as exc:
        # A poll failure doesn't mean the transfer failed - record it for
        # visibility without touching status, same as poll_transfer.
        workflow.repo_staging_error_message = f"Poll failed: {exc}"
        workflow.repo_staging_updated_at = datetime.now(UTC)
        db.add(workflow)
        db.commit()
        raise GlobusTransferError(f"Failed to poll workflow repo transfer: {exc}") from exc

    globus_status = task["status"]
    if globus_status == "SUCCEEDED":
        workflow.repo_staging_status = "completed"
    elif globus_status == "FAILED":
        workflow.repo_staging_status = "failed"
        fatal_error = task.get("fatal_error") or {}
        workflow.repo_staging_error_message = (
            fatal_error.get("description") or "Globus transfer failed"
        )
    else:
        return

    workflow.repo_staging_updated_at = datetime.now(UTC)
    db.add(workflow)
    db.commit()
    _promote_queued_jobs_waiting_on_repo(db, workflow)


def _promote_queued_jobs_waiting_on_repo(db: Session, workflow: Workflow) -> None:
    """Once a workflow's repo staging settles, re-check every run still
    waiting on it - a repo staging event can unblock several runs at once."""
    from ..db.models.job_queue import QueuedJob
    from .globus_transfer import _try_promote_staging_job  # local import: avoid import cycle

    queued_jobs = db.scalars(
        select(QueuedJob).where(QueuedJob.workflow_id == workflow.id, QueuedJob.status == "staging")
    ).all()
    for queued_job in queued_jobs:
        _try_promote_staging_job(db, queued_job)


@dataclass(frozen=True)
class RepoStagingSyncResult:
    """Outcome for one batch of workflow repo staging sync work."""

    checked: int
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    errored: int = 0


def sync_workflow_repo_staging(
    db: Session, *, settings: Settings | None = None
) -> RepoStagingSyncResult:
    """Submit pending and poll in-progress workflow repo stagings for one batch."""
    settings = settings or get_settings()
    workflows = list(
        db.scalars(
            select(Workflow).where(Workflow.repo_staging_status.in_(["pending", "in_progress"]))
        )
    )

    submitted = completed = failed = errored = 0
    for workflow in workflows:
        try:
            if workflow.repo_staging_status == "pending":
                stage_pending_repo(db, workflow, settings=settings)
                if workflow.repo_staging_status == "in_progress":
                    submitted += 1
            else:
                poll_repo_staging(db, workflow, settings=settings)
        except (RepoStagingError, GlobusTransferError) as exc:
            db.rollback()
            logger.warning("Failed to sync workflow repo staging for %s: %s", workflow.id, exc)
            errored += 1
            continue
        except Exception:
            db.rollback()
            logger.exception("Unexpected error syncing workflow repo staging for %s", workflow.id)
            errored += 1
            continue

        if workflow.repo_staging_status == "failed":
            failed += 1
        elif workflow.repo_staging_status == "completed":
            completed += 1

    return RepoStagingSyncResult(
        checked=len(workflows),
        submitted=submitted,
        completed=completed,
        failed=failed,
        errored=errored,
    )
