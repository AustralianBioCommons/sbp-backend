"""Stage a workflow's GitHub repo onto Gadi via S3 + Globus, cached by commit.

Gadi compute nodes have no network access, so Nextflow can't fetch pipeline
code from GitHub itself at run time - the repo has to already be sitting on
Gadi's filesystem, the same way input files are staged there via Globus. Unlike
input files, a repo checkout is shared across every run of a workflow, so the
staged copy is cached on the Workflow row itself (keyed by commit sha) rather
than duplicated per run: if the workflow's default_revision still resolves to
the same commit that's already staged/staging, launches reuse it for free.
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import globus_sdk
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import GlobusSettings, Settings, get_settings
from ..db.models.core import Workflow
from .globus_client import get_transfer_client
from .globus_errors import GlobusTransferError
from .globus_transfer import _gadi_relative_path
from .s3 import get_s3_client

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
_HTTP_TIMEOUT = 15
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


def resolve_latest_commit_sha(repo_url: str, revision: str) -> str:
    """Resolve a branch/tag/ref to its current commit sha via the GitHub API.

    One lightweight network call - no local clone needed - so this is cheap
    enough to run on every launch to check whether the cached staged copy is
    still current.
    """
    owner, repo = parse_github_repo(repo_url)
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/commits/{revision}"
    try:
        response = httpx.get(
            url, headers={"Accept": "application/vnd.github+json"}, timeout=_HTTP_TIMEOUT
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RepoStagingError(
            f"Failed to resolve commit for {owner}/{repo}@{revision}: {exc}"
        ) from exc
    sha = response.json().get("sha")
    if not sha:
        raise RepoStagingError(f"GitHub API returned no commit sha for {owner}/{repo}@{revision}")
    return cast(str, sha)


def build_repo_s3_prefix(owner: str, repo: str, commit_sha: str) -> str:
    return f"workflow-repos/{owner}-{repo}/{commit_sha}"


def build_repo_gadi_path(
    owner: str, repo: str, commit_sha: str, *, globus_settings: GlobusSettings
) -> str:
    # Seqera's launch API rejects `pipeline` (a "file:<path>" URL for a locally
    # staged checkout, see workflows.py) unless the path ends in ".git" -
    # confirmed by a known-working manually staged pipeline ending in ".git",
    # and by our own staged path (without it) being rejected with the same
    # "Invalid pipeline URL" error. Unconfirmed: whether this is purely a
    # string-format check on the `pipeline` value, or whether Nextflow later
    # also expects the directory itself to be a real git repo (with .git/
    # metadata) rather than a plain extracted checkout - revisit if a launch
    # gets past this validation but then fails once Nextflow actually tries
    # to load the pipeline from here.
    return f"{globus_settings.gadi_collection_root}/workflow_repos/{owner}-{repo}/{commit_sha}.git"


def ensure_repo_staging_requested(
    db: Session, workflow: Workflow, *, settings: Settings | None = None
) -> str:
    """Resolve the workflow's current commit, returning the Gadi path it will
    live at, and kick off staging in the background if that commit isn't
    already staged or being staged.

    Called synchronously during workflow launch - deliberately does only the
    one cheap GitHub API call here. The actual tarball download/upload/Globus
    transfer happens later via sync_workflow_repo_staging, the same
    submit-then-poll pattern already used for input file staging, so launch
    requests stay fast.
    """
    settings = settings or get_settings()
    owner, repo = parse_github_repo(workflow.repo_url)
    commit_sha = resolve_latest_commit_sha(workflow.repo_url, workflow.default_revision)
    gadi_path = build_repo_gadi_path(owner, repo, commit_sha, globus_settings=settings.globus)

    already_current = (
        workflow.repo_staged_commit_sha == commit_sha
        and workflow.repo_staging_status in ("pending", "in_progress", "completed")
    )
    if not already_current:
        workflow.repo_staged_commit_sha = commit_sha
        workflow.repo_staging_status = "pending"
        workflow.repo_gadi_path = gadi_path
        workflow.repo_staging_transfer_id = None
        workflow.repo_staging_error_message = None
        workflow.repo_staging_updated_at = datetime.now(UTC)
        db.add(workflow)
        db.commit()

    return gadi_path


def _download_and_upload_repo_tarball(
    owner: str, repo: str, commit_sha: str, s3_prefix: str, *, settings: Settings
) -> None:
    """Download the repo's tarball at commit_sha from GitHub and re-upload its
    contents to S3 under s3_prefix, one object per file.

    Uploading plain files (not the tarball itself) means the later Globus
    transfer can land them straight onto Gadi's filesystem with no unpack step
    required there - Gadi has no execution access for us to run one.
    """
    tarball_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/tarball/{commit_sha}"
    s3_client = get_s3_client(settings)
    bucket = settings.aws.s3_bucket

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / "repo.tar.gz"
        try:
            with httpx.stream(
                "GET", tarball_url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
            ) as response:
                response.raise_for_status()
                with archive_path.open("wb") as archive_file:
                    for chunk in response.iter_bytes():
                        archive_file.write(chunk)
        except httpx.HTTPError as exc:
            raise RepoStagingError(
                f"Failed to download tarball for {owner}/{repo}@{commit_sha}: {exc}"
            ) from exc

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")

        # GitHub's tarball API wraps the checkout in one top-level
        # "<owner>-<repo>-<short-sha>/" directory.
        extracted_roots = list(extract_dir.iterdir())
        if len(extracted_roots) != 1:
            raise RepoStagingError(f"Unexpected tarball layout for {owner}/{repo}@{commit_sha}")
        repo_root = extracted_roots[0]

        for file_path in repo_root.rglob("*"):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(repo_root).as_posix()
            s3_client.upload_file(str(file_path), bucket, f"{s3_prefix}/{relative_path}")


def stage_pending_repo(
    db: Session, workflow: Workflow, *, settings: Settings | None = None
) -> None:
    """Submit the S3 upload + Globus transfer for a "pending" workflow repo."""
    settings = settings or get_settings()
    owner, repo = parse_github_repo(workflow.repo_url)
    commit_sha = workflow.repo_staged_commit_sha
    if not commit_sha:
        raise RepoStagingError(f"Workflow {workflow.id} has no commit sha to stage")

    s3_prefix = build_repo_s3_prefix(owner, repo, commit_sha)
    gadi_path = workflow.repo_gadi_path or build_repo_gadi_path(
        owner, repo, commit_sha, globus_settings=settings.globus
    )

    try:
        _download_and_upload_repo_tarball(owner, repo, commit_sha, s3_prefix, settings=settings)

        transfer_client = get_transfer_client(settings.globus)
        # Same submission-id-reuse trick as DataTransfer.transfer_id: persist it
        # before submit_transfer so a crash-and-retry doesn't double-submit.
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
        # gadi_path is the real absolute Gadi filesystem path (also what's
        # handed to Nextflow as `pipeline`), but the Gadi collection's own root
        # maps to GLOBUS_GADI_COLLECTION_ROOT, not "/" - sending the absolute
        # path unchanged double-nests it under the collection root on the real
        # filesystem, same bug already fixed once for input file staging
        # (see _gadi_relative_path's docstring in globus_transfer.py).
        destination_path = _gadi_relative_path(gadi_path, globus_settings=settings.globus)
        transfer_data.add_item(f"/{s3_prefix}", destination_path, recursive=True)
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
        # A poll failure doesn't mean the underlying transfer failed - record it
        # for visibility without touching status, same as poll_transfer.
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
    waiting on it - unlike input-file staging (gated per run via
    _notify_launcher), a repo staging event can unblock several runs at once."""
    from ..db.models.job_queue import QueuedJob
    from .globus_transfer import _try_promote_staging_job  # local import: avoid import cycle

    queued_jobs = db.scalars(
        select(QueuedJob).where(
            QueuedJob.workflow_id == workflow.id, QueuedJob.status == "staging"
        )
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
