"""Stage a workflow's GitHub repo onto Gadi via S3 + Globus, cached by
(revision, commit) pair.

Gadi compute nodes have no network access, so under NXF_OFFLINE Nextflow
resolves `pipeline` (the workflow's GitHub URL) against a pre-staged checkout
on Gadi's filesystem instead of fetching it, the same way input files are
staged via Globus. A checkout is shared across every run of a workflow, so
it's cached on the workflow/backend repository-status row. If default_revision
still resolves to the staged/staging commit, launches reuse the checkout.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import globus_sdk
from github import Auth, Github, GithubException, GithubIntegration
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import GlobusSettings, Settings, get_settings
from ..db.models import ComputeBackend, WorkflowRepoStatus
from ..db.models.core import Workflow
from ..schemas.backend import RepositoryStatus
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


def build_repo_assets_s3_prefix(owner: str, repo: str, revision: str, commit_sha: str) -> str:
    return f"workflow-repos-assets/{owner}-{repo}/{revision}/{commit_sha}"


def build_repo_gadi_path(
    owner: str, repo: str, revision: str, commit_sha: str, *, globus_settings: GlobusSettings
) -> str:
    """Path to a real (non-bare) clone of the commit - used for reading
    pipeline-bundled asset files off disk, and as the checkout Gadi launches
    resolve `pipeline` against under NXF_OFFLINE.

    revision is part of the path because the checkout only gets a ref named
    after it (see stage_pending_repo's refspec) - two revisions can resolve
    to the same commit_sha, and without revision here they'd collide on one
    checkout with only one of their refs.
    """
    return (
        f"{globus_settings.collection_root}/workflow_repos/"
        f"{repo}/{revision}/{commit_sha}/{owner}/{repo}"
    )


@dataclass(frozen=True)
class RepoStagingLocations:
    """Where a workflow's staged checkout lives on Gadi (see
    build_repo_gadi_path). gadi_path and assets_gadi_path are always equal -
    kept as two fields since callers use them for different purposes."""

    gadi_path: str
    assets_gadi_path: str


def get_repository_globus_settings(backend: ComputeBackend) -> GlobusSettings:
    globus = backend.get_settings().globus
    if globus is None:
        raise RepoStagingError(f"Backend '{backend.name}' has no repository Globus settings")
    return globus


def ensure_repo_staging_requested(
    db: Session, workflow: Workflow, *, backend: ComputeBackend, settings: Settings | None = None
) -> RepoStagingLocations:
    """Resolve the checkout and request staging for this workflow/backend pair."""
    settings = settings or get_settings()
    globus_settings = get_repository_globus_settings(backend)
    owner, repo = parse_github_repo(workflow.repo_url)
    commit_sha = resolve_latest_commit_sha(
        workflow.repo_url, workflow.default_revision, settings=settings
    )
    location = build_repo_gadi_path(
        owner, repo, workflow.default_revision, commit_sha, globus_settings=globus_settings
    )
    try:
        WorkflowRepoStatus.request_staging(
            db, backend, workflow, location=location, commit_sha=commit_sha
        )
    except ValueError as exc:
        raise RepoStagingError(str(exc)) from exc
    db.commit()
    return RepoStagingLocations(gadi_path=location, assets_gadi_path=location)


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


def _clone_working_checkout(bare_dir: str, dest_dir: str, *, origin_url: str) -> None:
    """Clone a real (non-bare) working checkout from the already-fetched bare
    repo (local filesystem clone, no second network fetch). A real `.git/`,
    unlike a `git archive` extract, so it's indistinguishable from a checkout
    Nextflow would produce itself (see build_repo_gadi_path).

    `git clone` records bare_dir (an ephemeral tmp dir) as `origin`, so it
    must be rewritten to origin_url (the workflow's real repo_url) -
    otherwise Nextflow sees a since-deleted tmp path as this checkout's
    provenance and refuses to reuse it ("has already been downloaded from a
    different provider")."""
    _run_git("clone", bare_dir, dest_dir, cwd=bare_dir)
    _run_git("remote", "set-url", "origin", origin_url, cwd=dest_dir)


def _clone_and_upload_repo(
    owner: str,
    repo: str,
    commit_sha: str,
    repo_url: str,
    *,
    revision: str,
    settings: Settings,
) -> None:
    """Fetch commit_sha and upload a real working checkout to S3 under
    build_repo_assets_s3_prefix, for reading asset files and as the checkout
    Gadi launches resolve `pipeline` against.

    Fetches into a branch named after `revision` since a shallow bare repo
    has no other branches and Seqera's `revision` must reference a real ref.
    tmp_dir (the bare repo) never leaves local disk - it's only a clone
    source for the working checkout below, so no second network fetch.
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
            # Pack the shallow fetch's loose objects before cloning - a real
            # pipeline's tree can be hundreds of small files otherwise.
            _run_git("gc", cwd=tmp_dir)
        except RepoStagingError as exc:
            raise RepoStagingError(f"Failed to clone {owner}/{repo}@{commit_sha}: {exc}") from exc

        assets_s3_prefix = build_repo_assets_s3_prefix(owner, repo, revision, commit_sha)
        with tempfile.TemporaryDirectory() as assets_tmp_dir:
            try:
                _clone_working_checkout(tmp_dir, assets_tmp_dir, origin_url=repo_url)
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
    db: Session, repo_status: WorkflowRepoStatus, *, settings: Settings | None = None
) -> None:
    """Submit the S3 upload and Globus transfer for one pending repository."""
    settings = settings or get_settings()
    workflow = repo_status.workflow
    globus_settings = get_repository_globus_settings(repo_status.compute_backend)
    owner, repo = parse_github_repo(workflow.repo_url)
    commit_sha = repo_status.staged_commit_sha
    if not commit_sha:
        raise RepoStagingError(f"Workflow {workflow.id} has no commit sha to stage")
    assets_s3_prefix = build_repo_assets_s3_prefix(
        owner, repo, workflow.default_revision, commit_sha
    )
    try:
        _clone_and_upload_repo(
            owner,
            repo,
            commit_sha,
            workflow.repo_url,
            revision=workflow.default_revision,
            settings=settings,
        )
        transfer_client = get_transfer_client(globus_settings)
        submission_id = repo_status.transfer_id
        if not submission_id:
            submission_id = cast(str, transfer_client.get_submission_id()["value"])
            repo_status.record_submission(submission_id)
            db.commit()
        transfer_data = globus_sdk.TransferData(
            globus_settings.s3_collection_id,
            globus_settings.backend_collection_id,
            submission_id=submission_id,
            label=f"sbp-repo-{owner}-{repo}-{commit_sha[:12]}",
        )
        destination_path = _gadi_relative_path(
            repo_status.location, globus_settings=globus_settings
        )
        transfer_data.add_item(f"/{assets_s3_prefix}", destination_path, recursive=True)
        result = transfer_client.submit_transfer(transfer_data)
    except (RepoStagingError, globus_sdk.GlobusAPIError) as exc:
        logger.warning("Workflow repo staging failed for %s: %s", repo_status.workflow_id, exc)
        repo_status.mark_failed(str(exc))
        db.commit()
        return
    repo_status.mark_in_progress(result["task_id"])
    db.commit()


def poll_repo_staging(
    db: Session, repo_status: WorkflowRepoStatus, *, settings: Settings | None = None
) -> None:
    """Poll the existing Globus task for one workflow/backend checkout."""
    if not repo_status.transfer_id:
        raise GlobusTransferError(f"Workflow {repo_status.workflow_id} has no transfer_id to poll")
    transfer_client = get_transfer_client(
        get_repository_globus_settings(repo_status.compute_backend)
    )
    try:
        task = transfer_client.get_task(repo_status.transfer_id)
    except globus_sdk.GlobusAPIError as exc:
        repo_status.record_error(f"Poll failed: {exc}")
        db.commit()
        raise GlobusTransferError(f"Failed to poll workflow repo transfer: {exc}") from exc
    if task["status"] == "SUCCEEDED":
        repo_status.mark_completed()
    elif task["status"] == "FAILED":
        fatal_error = task.get("fatal_error") or {}
        repo_status.mark_failed(fatal_error.get("description") or "Globus transfer failed")
    else:
        return
    db.commit()
    _promote_queued_jobs_waiting_on_repo(db, repo_status)


def _promote_queued_jobs_waiting_on_repo(db: Session, repo_status: WorkflowRepoStatus) -> None:
    """Recheck queued runs for the workflow and backend whose staging settled."""
    from ..db.models import WorkflowRun
    from ..db.models.job_queue import QueuedJob
    from .globus_transfer import _try_promote_staging_job

    queued_jobs = db.scalars(
        select(QueuedJob)
        .join(WorkflowRun, QueuedJob.workflow_run_id == WorkflowRun.id)
        .where(
            QueuedJob.workflow_id == repo_status.workflow_id,
            WorkflowRun.backend_id == repo_status.compute_backend_id,
            QueuedJob.status == "staging",
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
    repos = list(
        db.scalars(
            select(WorkflowRepoStatus).where(
                WorkflowRepoStatus.status.in_(
                    [RepositoryStatus.PENDING, RepositoryStatus.IN_PROGRESS]
                )
            )
        )
    )

    submitted = completed = failed = errored = 0
    for repo_status in repos:
        try:
            if repo_status.status == "pending":
                stage_pending_repo(db, repo_status, settings=settings)
                if repo_status.status == "in_progress":
                    submitted += 1
            else:
                poll_repo_staging(db, repo_status, settings=settings)
        except (RepoStagingError, GlobusTransferError) as exc:
            db.rollback()
            logger.warning(
                "Failed to sync workflow repo staging for %s: %s", repo_status.workflow_id, exc
            )
            errored += 1
            continue
        except Exception:
            db.rollback()
            logger.exception(
                "Unexpected error syncing workflow repo staging for %s", repo_status.workflow_id
            )
            errored += 1
            continue

        if repo_status.status == "failed":
            failed += 1
        elif repo_status.status == "completed":
            completed += 1

    return RepoStagingSyncResult(
        checked=len(repos),
        submitted=submitted,
        completed=completed,
        failed=failed,
        errored=errored,
    )
