"""Tests for staging a workflow's GitHub repo onto Gadi via S3 + Globus."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from github import GithubException

from app.config import GlobusSettings
from app.db.models import WorkflowRepoStatus
from app.services.globus_errors import GlobusTransferError
from app.services.workflow_repo_staging import (
    RepoStagingError,
    build_repo_checkout_path,
    parse_github_repo,
    poll_repo_staging,
    prepare_repository,
    resolve_latest_commit_sha,
    stage_pending_repo,
    sync_workflow_repo_staging,
)
from tests.datagen import QueuedJobFactory, WorkflowFactory, WorkflowRepoStatusFactory


def _mock_github_client(*, sha: str | None = None, raises: Exception | None = None) -> MagicMock:
    """PyGithub client double - resolve_latest_commit_sha only ever calls
    client.get_repo(...).get_commit(...).sha, so that's all this needs to
    fake. PyGithub uses `requests` internally (not httpx), so respx can't
    intercept its calls - mocking at this boundary is simpler than pulling in
    another HTTP-mocking library just for these few tests."""
    client = MagicMock()
    if raises is not None:
        client.get_repo.return_value.get_commit.side_effect = raises
    else:
        commit = MagicMock()
        commit.sha = sha
        client.get_repo.return_value.get_commit.return_value = commit
    return client


def _globus_api_error(status_code: int, json_body: dict) -> Exception:
    """Same helper as test_services_globus_transfer.py - builds a real
    globus_sdk.TransferAPIError, which needs a full requests.Response."""
    import globus_sdk

    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(json_body).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.request = requests.PreparedRequest()
    response.request.prepare(method="GET", url="https://transfer.api.globus.org/v0.10/x")
    return globus_sdk.TransferAPIError(response)


def _make_local_git_repo(tmp_path: Path, files: dict[str, str]) -> tuple[str, str]:
    """Create a real local git repo (used as a stand-in "GitHub" remote so
    _clone_and_upload_repo's git commands run for real, not mocked) and return
    (repo_path, commit_sha)."""
    repo_dir = tmp_path / "origin"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    for relative_path, content in files.items():
        file_path = repo_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        subprocess.run(["git", "add", relative_path], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo_dir,
        check=True,
    )
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return str(repo_dir), commit_sha


@pytest.fixture
def repo_status_factory(default_backend, monkeypatch, mock_settings):
    for field, value in mock_settings.globus.model_dump().items():
        monkeypatch.setenv(f"GADI_GLOBUS_{field.upper()}", str(value))

    def create_repo_status(
        repo_url="https://github.com/test/repo", default_revision="dev", **kwargs
    ):
        workflow = WorkflowFactory.create_sync(repo_url=repo_url, default_revision=default_revision)
        values = {
            "location": "/test/workflow_repos/test-repo/dev/abc123",
            "status": "pending",
            "staged_commit_sha": "abc123",
            "transfer_id": None,
            "error_message": None,
        }
        values.update(kwargs)
        return WorkflowRepoStatusFactory.create_sync(
            workflow=workflow, compute_backend=default_backend, **values
        )

    return create_repo_status


@pytest.fixture
def mock_transfer_client():
    client = MagicMock()
    with patch("app.services.workflow_repo_staging.get_transfer_client", return_value=client):
        yield client


@pytest.fixture
def globus_settings():
    return GlobusSettings(
        client_id="test-globus-client-id",
        client_secret="test-globus-client-secret",
        gadi_collection_id="test-gadi-collection-id",
        s3_collection_id="test-s3-collection-id",
        gadi_collection_root="/test",
        input_dir="/test/input",
        output_dir="/test/output",
    )


# ============================================================================
# parse_github_repo / build_repo_* path helpers
# ============================================================================


def test_parse_github_repo_extracts_owner_and_repo():
    assert parse_github_repo("https://github.com/nf-core/proteinfold") == (
        "nf-core",
        "proteinfold",
    )


def test_parse_github_repo_strips_git_suffix():
    assert parse_github_repo("https://github.com/nf-core/proteinfold.git") == (
        "nf-core",
        "proteinfold",
    )


def test_parse_github_repo_rejects_non_github_host():
    with pytest.raises(RepoStagingError, match="Only github.com"):
        parse_github_repo("https://gitlab.com/nf-core/proteinfold")


def test_parse_github_repo_rejects_missing_repo():
    with pytest.raises(RepoStagingError, match="Could not parse"):
        parse_github_repo("https://github.com/nf-core")


def test_build_repo_checkout_path(globus_settings):
    path = build_repo_checkout_path(
        "nf-core", "proteinfold", "dev", "abc123", globus_settings=globus_settings
    )
    assert path == "/test/workflow_repos/proteinfold/dev/abc123/nf-core/proteinfold"


def test_build_repo_checkout_path_distinguishes_revisions_at_same_commit(globus_settings):
    """Two revisions resolving to the same commit must not collide on one
    checkout path - each name needs its own staged copy with its own ref."""
    main_path = build_repo_checkout_path(
        "nf-core", "proteinfold", "main", "abc123", globus_settings=globus_settings
    )
    staging_path = build_repo_checkout_path(
        "nf-core", "proteinfold", "staging", "abc123", globus_settings=globus_settings
    )
    assert main_path != staging_path


# ============================================================================
# resolve_latest_commit_sha
# ============================================================================


def test_resolve_latest_commit_sha_success(mock_settings):
    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="abc123"),
    ):
        assert (
            resolve_latest_commit_sha(
                "https://github.com/nf-core/proteinfold", "dev", settings=mock_settings
            )
            == "abc123"
        )


def test_resolve_latest_commit_sha_http_error_raises(mock_settings):
    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(raises=GithubException(404, {"message": "Not Found"}, {})),
    ):
        with pytest.raises(RepoStagingError, match="Failed to resolve commit"):
            resolve_latest_commit_sha(
                "https://github.com/nf-core/proteinfold", "dev", settings=mock_settings
            )


def test_resolve_latest_commit_sha_missing_sha_raises(mock_settings):
    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha=None),
    ):
        with pytest.raises(RepoStagingError, match="no commit sha"):
            resolve_latest_commit_sha(
                "https://github.com/nf-core/proteinfold", "dev", settings=mock_settings
            )


# ============================================================================
# prepare_repository
# ============================================================================


def test_prepare_repository_marks_pending_on_cache_miss(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    repo = repo_status_factory()
    workflow = repo.workflow
    backend = repo.compute_backend
    test_db.delete(repo)
    test_db.commit()

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="newsha"),
    ):
        locations = prepare_repository(test_db, workflow, backend=backend, settings=mock_settings)

    repo = WorkflowRepoStatus.get_for_workflow(test_db, backend.id, workflow.id)
    assert locations.location == repo.location
    assert locations.location == "/test/workflow_repos/repo/dev/newsha/test/repo"
    assert repo.staged_commit_sha == "newsha"
    assert repo.status == "pending"


def test_prepare_repository_resets_on_commit_change(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="oldsha",
        status="completed",
        location="/test/workflow_repos/test-repo/dev/oldsha.git",
        transfer_id="old-task-id",
        error_message=None,
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="newsha"),
    ):
        prepare_repository(
            test_db, workflow.workflow, backend=workflow.compute_backend, settings=mock_settings
        )

    assert workflow.staged_commit_sha == "newsha"
    assert workflow.status == "pending"
    assert workflow.transfer_id is None


def test_prepare_repository_reuses_cache_hit(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    """Same commit, already completed - must not reset back to pending (that
    would re-trigger staging for no reason)."""
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="samesha",
        status="completed",
        location="/test/workflow_repos/repo/dev/samesha/test/repo",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        locations = prepare_repository(
            test_db, workflow.workflow, backend=workflow.compute_backend, settings=mock_settings
        )

    assert locations.location == "/test/workflow_repos/repo/dev/samesha/test/repo"
    assert locations.location == "/test/workflow_repos/repo/dev/samesha/test/repo"
    assert workflow.status == "completed"


def test_prepare_repository_restages_on_revision_change_same_commit(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    """Regression test: a freshly-cut branch ("staging") can resolve to the
    exact same commit as the previously staged revision ("main"). Comparing
    commit_sha alone would wrongly call this "up to date" and reuse a
    checkout whose only ref is named "main", so a Nextflow launch asking for
    revision "staging" would fail with `invalid object name 'staging'`."""
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="staging",
        staged_commit_sha="samesha",
        status="completed",
        location="/test/workflow_repos/repo/main/samesha/test/repo",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        locations = prepare_repository(
            test_db, workflow.workflow, backend=workflow.compute_backend, settings=mock_settings
        )

    assert locations.location == "/test/workflow_repos/repo/staging/samesha/test/repo"
    assert workflow.location == "/test/workflow_repos/repo/staging/samesha/test/repo"
    assert workflow.status == "pending"


def test_prepare_repository_retries_after_failure(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    """Same commit but previously failed - must re-request staging, not treat
    the failure as a permanent cache entry."""
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="samesha",
        status="failed",
        error_message="boom",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        prepare_repository(
            test_db, workflow.workflow, backend=workflow.compute_backend, settings=mock_settings
        )

    assert workflow.status == "pending"
    assert workflow.error_message is None


# ============================================================================
# stage_pending_repo
# ============================================================================


def test_stage_pending_repo_uses_collection_relative_destination_path(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    """Regression test: add_item must receive the Gadi path relative to the
    collection root, not the raw absolute Gadi filesystem path - sending the
    absolute path double-nests it under the collection root on the real
    filesystem (confirmed in production: the staged repo was unreachable at
    its expected path)."""
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}

    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="abc123",
        status="pending",
        location="/test/workflow_repos/test-repo/dev/abc123",
        transfer_id=None,
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo") as mock_clone:
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    mock_clone.assert_called_once()
    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["source_endpoint"] == "test-s3-collection-id"
    assert submitted["destination_endpoint"] == "test-gadi-collection-id"
    # NOT "/test/workflow_repos/test-repo/dev/abc123" - that's the absolute
    # path, which would double-nest under the "/test" collection root on Gadi.
    assert submitted["DATA"][0]["destination_path"] == "/workflow_repos/test-repo/dev/abc123"
    assert submitted["DATA"][0]["source_path"] == "/workflow-repos-assets/test-repo/dev/abc123"
    assert submitted["DATA"][0]["recursive"] is True
    assert len(submitted["DATA"]) == 1

    assert workflow.status == "in_progress"
    assert workflow.transfer_id == "task-1"


def test_stage_pending_repo_no_commit_sha_raises(
    test_db, persistent_models, mock_settings, repo_status_factory
):
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        staged_commit_sha=None,
    )
    with pytest.raises(RepoStagingError, match="no commit sha"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)


def test_stage_pending_repo_download_failure_marks_failed(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="abc123",
        status="pending",
    )

    with patch(
        "app.services.workflow_repo_staging._clone_and_upload_repo",
        side_effect=RepoStagingError("clone failed"),
    ):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.status == "failed"
    assert "clone failed" in workflow.error_message
    mock_transfer_client.submit_transfer.assert_not_called()


def test_stage_pending_repo_submission_api_error_marks_failed(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.side_effect = _globus_api_error(
        400, {"code": "UNKNOWN_SCOPE_ERROR", "message": "bad scope"}
    )
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="abc123",
        status="pending",
        location="/test/workflow_repos/test-repo/dev/abc123",
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.status == "failed"
    assert "UNKNOWN_SCOPE_ERROR" in workflow.error_message


def test_stage_pending_repo_reuses_existing_submission_id(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}
    workflow = repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="abc123",
        status="pending",
        location="/test/workflow_repos/test-repo/dev/abc123",
        transfer_id="already-committed-sub-id",
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    mock_transfer_client.get_submission_id.assert_not_called()
    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["submission_id"] == "already-committed-sub-id"


# ============================================================================
# _clone_and_upload_repo
# ============================================================================


def test_clone_and_upload_repo_uploads_plain_checkout_assets(tmp_path, mock_settings):
    """A real (non-bare) checkout must be uploaded under
    build_repo_assets_s3_prefix - readable plain files for pipeline-bundled
    assets (e.g. bindcraft's settings JSON). Must include a real `.git/` (a
    `git clone`, not a `git archive` extract) or Nextflow fails with
    "Repository may be corrupted" when it finds this checkout at its own
    local-checkout slot."""
    from app.services.workflow_repo_staging import (
        _clone_and_upload_repo,
        build_repo_assets_s3_prefix,
    )

    repo_path, commit_sha = _make_local_git_repo(
        tmp_path,
        {
            "main.nf": "process {}",
            "assets/bindcraft/default_filters.json": '{"filter": true}',
        },
    )
    mock_s3_client = MagicMock()
    assets_prefix = build_repo_assets_s3_prefix("test", "repo", "dev", commit_sha)
    # Source files live under a TemporaryDirectory cleaned up before the
    # function returns - capture content at upload time, mocked here in
    # place of a real S3 PUT.
    uploaded_content: dict[str, bytes] = {}

    def _capture_upload(source_path: str, _bucket: str, key: str) -> None:
        uploaded_content[key] = Path(source_path).read_bytes()

    mock_s3_client.upload_file.side_effect = _capture_upload

    with patch("app.services.workflow_repo_staging.get_s3_client", return_value=mock_s3_client):
        _clone_and_upload_repo(
            "test",
            "repo",
            commit_sha,
            repo_path,
            revision="dev",
            settings=mock_settings,
        )

    assert f"{assets_prefix}/main.nf" in uploaded_content
    assert f"{assets_prefix}/assets/bindcraft/default_filters.json" in uploaded_content
    # Plain files, readable directly - not blobs needing `git show` to extract.
    assert uploaded_content[f"{assets_prefix}/main.nf"] == b"process {}"
    assert (
        uploaded_content[f"{assets_prefix}/assets/bindcraft/default_filters.json"]
        == b'{"filter": true}'
    )
    # A real .git/ must be present (see docstring above).
    assert f"{assets_prefix}/.git/config" in uploaded_content
    assert f"{assets_prefix}/.git/HEAD" in uploaded_content
    # `git clone` records its source (an ephemeral tmp dir) as `origin` by
    # default - must be rewritten to the workflow's real repo_url before
    # upload, or Nextflow rejects the checkout's stale provenance (see
    # _clone_working_checkout).
    git_config = uploaded_content[f"{assets_prefix}/.git/config"].decode()
    assert repo_path in git_config


def test_clone_and_upload_repo_clone_failure_raises(tmp_path, mock_settings):
    from app.services.workflow_repo_staging import _clone_and_upload_repo

    nonexistent_repo_path = str(tmp_path / "does-not-exist")

    with pytest.raises(RepoStagingError, match="Failed to clone"):
        _clone_and_upload_repo(
            "test",
            "repo",
            "abc123",
            nonexistent_repo_path,
            revision="dev",
            settings=mock_settings,
        )


# ============================================================================
# poll_repo_staging
# ============================================================================


def test_poll_repo_staging_no_transfer_id_raises(test_db, persistent_models, repo_status_factory):
    workflow = repo_status_factory(transfer_id=None)
    with pytest.raises(GlobusTransferError, match="no transfer_id"):
        poll_repo_staging(test_db, workflow)


def test_poll_repo_staging_succeeded_marks_completed_and_promotes_queued_jobs(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    workflow = repo_status_factory(transfer_id="task-1", status="in_progress")
    QueuedJobFactory.create_sync(workflow=workflow.workflow, status="staging")

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.status == "completed"


def test_poll_repo_staging_failed_records_fatal_error(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.return_value = {
        "status": "FAILED",
        "fatal_error": {"description": "permission denied"},
    }
    workflow = repo_status_factory(transfer_id="task-1", status="in_progress")

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.status == "failed"
    assert workflow.error_message == "permission denied"


def test_poll_repo_staging_active_leaves_status_unchanged(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.return_value = {"status": "ACTIVE"}
    workflow = repo_status_factory(transfer_id="task-1", status="in_progress")

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.status == "in_progress"


def test_poll_repo_staging_api_error_records_message_without_changing_status(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.side_effect = _globus_api_error(
        401, {"code": "AuthenticationFailed", "message": "token expired"}
    )
    workflow = repo_status_factory(transfer_id="task-1", status="in_progress")

    with pytest.raises(GlobusTransferError, match="Failed to poll"):
        poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.status == "in_progress"
    assert "Poll failed" in workflow.error_message


# ============================================================================
# sync_workflow_repo_staging
# ============================================================================


def test_sync_workflow_repo_staging_submits_pending(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}
    repo_status_factory(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        staged_commit_sha="abc123",
        status="pending",
        location="/test/workflow_repos/test-repo/abc123",
        transfer_id=None,
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo"):
        result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.submitted == 1


def test_sync_workflow_repo_staging_polls_in_progress(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    repo_status_factory(transfer_id="task-1", status="in_progress")

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.completed == 1


def test_sync_workflow_repo_staging_ignores_workflows_not_staging(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    repo_status_factory(status="completed")
    WorkflowFactory.create_sync()

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 0
    mock_transfer_client.get_task.assert_not_called()


def test_sync_workflow_repo_staging_continues_after_unexpected_error(
    test_db, persistent_models, mock_transfer_client, mock_settings, repo_status_factory
):
    mock_transfer_client.get_task.side_effect = RuntimeError("boom")
    repo_status_factory(transfer_id="task-1", status="in_progress")

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.errored == 1


def test_request_does_not_overwrite_active_transfer_for_different_commit(
    test_db,
    repo_status_factory,
    mock_settings,
):
    repo = repo_status_factory(status="in_progress", transfer_id="active-task")
    original_location = repo.location
    original_sha = repo.staged_commit_sha
    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="different-sha"),
    ):
        with pytest.raises(RepoStagingError, match="already active"):
            prepare_repository(
                test_db,
                repo.workflow,
                backend=repo.compute_backend,
                settings=mock_settings,
            )
    test_db.refresh(repo)
    assert repo.location == original_location
    assert repo.staged_commit_sha == original_sha
    assert repo.transfer_id == "active-task"
    assert repo.status == "in_progress"


def test_poll_promotes_only_runs_on_the_matching_backend(
    test_db,
    repo_status_factory,
    mock_transfer_client,
    mock_settings,
):
    from app.schemas.backend import ComputeBackendName
    from app.services.job_queue import promote_ready_job
    from tests.datagen import AppUserFactory, ComputeBackendFactory, WorkflowRunFactory

    repo = repo_status_factory(status="in_progress", transfer_id="active-task")
    other_backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    other_repo = WorkflowRepoStatusFactory.create_sync(
        workflow=repo.workflow,
        compute_backend=other_backend,
        status="failed",
        location="/other/backend/checkout",
        error_message="other backend failure",
    )
    user = AppUserFactory.create_sync()
    jobs = []
    for backend in (repo.compute_backend, other_backend):
        run = WorkflowRunFactory.create_sync(workflow=repo.workflow, backend=backend, owner=user)
        jobs.append(
            QueuedJobFactory.create_sync(
                workflow=repo.workflow,
                workflow_run=run,
                status="staging",
                repository_transfer="globus",
                repository_location=(
                    repo.location if backend == repo.compute_backend else other_repo.location
                ),
                repository_ready=False,
            )
        )
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    poll_repo_staging(test_db, repo, settings=mock_settings)
    test_db.refresh(jobs[0])
    test_db.refresh(jobs[1])
    assert jobs[0].status == "pending"
    assert jobs[1].status == "staging"
    assert other_repo.status == "failed"
    promote_ready_job(test_db, jobs[1])
    assert jobs[1].status == "failed"
    assert "other backend failure" in jobs[1].error


def test_request_creates_independent_state_for_each_backend(test_db, repo_status_factory):
    from app.schemas.backend import ComputeBackendName
    from tests.datagen import ComputeBackendFactory

    repo = repo_status_factory(status="completed", staged_commit_sha="old-commit")
    other_backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    other_repo = WorkflowRepoStatus.request_staging(
        test_db,
        other_backend,
        repo.workflow,
        location="/other/backend/checkout",
        commit_sha="new-commit",
    )
    test_db.commit()
    assert other_repo.compute_backend_id == other_backend.id
    assert other_repo.status == "pending"
    assert repo.status == "completed"
    assert repo.staged_commit_sha == "old-commit"


@pytest.mark.parametrize("existing", [False, True])
def test_native_repository_bypasses_staging(test_db, repo_status_factory, mocker, existing):
    from app.schemas.backend import RepositoryTransferMode

    repo = repo_status_factory(status="failed", error_message="old failure")
    workflow, backend = repo.workflow, repo.compute_backend
    if not existing:
        test_db.delete(repo)
        test_db.commit()
    backend.repository_transfer = RepositoryTransferMode.NATIVE
    resolve = mocker.patch("app.services.workflow_repo_staging.resolve_latest_commit_sha")
    globus = mocker.patch("app.services.workflow_repo_staging.get_repository_globus_settings")
    upload = mocker.patch("app.services.workflow_repo_staging._clone_and_upload_repo")
    result = prepare_repository(test_db, workflow, backend=backend)
    assert result.mode == RepositoryTransferMode.NATIVE
    assert result.location is None
    assert result.ready
    resolve.assert_not_called()
    globus.assert_not_called()
    upload.assert_not_called()
    if existing:
        assert repo.status == "failed"
    else:
        assert WorkflowRepoStatus.get_for_workflow(test_db, backend.id, workflow.id) is None


def test_old_checkout_remains_ready_while_inputs_wait_and_cache_refreshes(
    test_db,
    repo_status_factory,
    mock_transfer_client,
    mock_settings,
):
    from app.services.job_queue import promote_ready_job
    from tests.datagen import AppUserFactory, DataTransferFactory, WorkflowRunFactory

    repo = repo_status_factory(status="in_progress", transfer_id="old-task")
    old_location = repo.location
    run = WorkflowRunFactory.create_sync(
        workflow=repo.workflow,
        backend=repo.compute_backend,
        owner=AppUserFactory.create_sync(),
    )
    job = QueuedJobFactory.create_sync(
        workflow=repo.workflow,
        workflow_run=run,
        status="staging",
        repository_transfer="globus",
        repository_location=old_location,
        repository_ready=False,
    )
    transfer = DataTransferFactory.create_sync(
        workflow_run=run, direction="input", status="pending"
    )
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    poll_repo_staging(test_db, repo, settings=mock_settings)
    assert job.repository_ready
    assert job.status == "staging"
    WorkflowRepoStatus.request_staging(
        test_db,
        repo.compute_backend,
        repo.workflow,
        location="/new/checkout",
        commit_sha="new-commit",
    )
    repo.mark_failed("new checkout failed")
    test_db.commit()
    transfer.status = "completed"
    test_db.commit()
    promote_ready_job(test_db, job)
    assert job.status == "pending"
    assert job.repository_location == old_location


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_different_checkout_does_not_satisfy_or_fail_repository_gate(
    test_db,
    repo_status_factory,
    status,
):
    from app.services.job_queue import promote_ready_job
    from tests.datagen import AppUserFactory, WorkflowRunFactory

    repo = repo_status_factory(status=status)
    run = WorkflowRunFactory.create_sync(
        workflow=repo.workflow, backend=repo.compute_backend, owner=AppUserFactory.create_sync()
    )
    job = QueuedJobFactory.create_sync(
        workflow=repo.workflow,
        workflow_run=run,
        status="staging",
        repository_transfer="globus",
        repository_location="/different/checkout",
        repository_ready=False,
    )
    promote_ready_job(test_db, job)
    assert job.status == "staging"
    assert not job.repository_ready


def test_native_job_ignores_stale_failed_globus_status(test_db, repo_status_factory):
    from app.services.job_queue import promote_ready_job
    from tests.datagen import AppUserFactory, WorkflowRunFactory

    repo = repo_status_factory(status="failed", error_message="stale error")
    run = WorkflowRunFactory.create_sync(
        workflow=repo.workflow, backend=repo.compute_backend, owner=AppUserFactory.create_sync()
    )
    job = QueuedJobFactory.create_sync(
        workflow=repo.workflow,
        workflow_run=run,
        status="staging",
        repository_transfer="native",
        repository_ready=True,
    )
    promote_ready_job(test_db, job)
    assert job.status == "pending"


def test_worker_rechecks_state_before_submitting_again(
    test_db,
    repo_status_factory,
    mock_transfer_client,
    mock_settings,
):
    repo = repo_status_factory()
    mock_transfer_client.get_submission_id.return_value = {"value": "one-submission"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "one-task"}
    with patch("app.services.workflow_repo_staging._clone_and_upload_repo") as clone:
        stage_pending_repo(test_db, repo, settings=mock_settings)
        stage_pending_repo(test_db, repo, settings=mock_settings)
    clone.assert_called_once()
    mock_transfer_client.submit_transfer.assert_called_once()
    assert repo.transfer_id == "one-task"


def test_pair_lock_is_stable_and_acquired_before_reading(mocker):
    from uuid import uuid4

    from sqlalchemy.orm import Session

    session = mocker.Mock(spec=Session)
    session.get_bind.return_value.dialect.name = "postgresql"
    session.scalar.return_value = None
    backend_id, workflow_id = uuid4(), uuid4()
    WorkflowRepoStatus.lock_for_workflow(session, backend_id, workflow_id)
    first_key = session.execute.call_args.args[0].compile().params
    WorkflowRepoStatus.lock_for_workflow(session, backend_id, workflow_id)
    assert session.execute.call_args.args[0].compile().params == first_key
    assert "pg_advisory_xact_lock" in str(session.execute.call_args.args[0])
    calls = [call[0] for call in session.mock_calls]
    assert calls.index("execute") < calls.index("scalar")
    WorkflowRepoStatus.lock_for_workflow(session, backend_id, uuid4())
    assert session.execute.call_args.args[0].compile().params != first_key


def test_worker_preserves_queued_repository_url_and_revision(
    test_db,
    repo_status_factory,
    mock_transfer_client,
    mock_settings,
):
    from tests.datagen import AppUserFactory, WorkflowRunFactory

    repo = repo_status_factory()
    run = WorkflowRunFactory.create_sync(
        workflow=repo.workflow, backend=repo.compute_backend, owner=AppUserFactory.create_sync()
    )
    QueuedJobFactory.create_sync(
        workflow=repo.workflow,
        workflow_run=run,
        status="staging",
        repository_transfer="globus",
        repository_location=repo.location,
        repository_ready=False,
        launch_payload={"pipeline": repo.workflow.repo_url, "revision": "dev"},
    )
    repo.workflow.repo_url = "https://github.com/other/newrepo"
    repo.workflow.default_revision = "new-branch"
    test_db.commit()
    mock_transfer_client.get_submission_id.return_value = {"value": "old-submission"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "old-task"}
    with patch("app.services.workflow_repo_staging._clone_and_upload_repo") as clone:
        stage_pending_repo(test_db, repo, settings=mock_settings)
    assert clone.call_args.args[3] == "https://github.com/test/repo"
    assert clone.call_args.kwargs["revision"] == "dev"
    submitted = mock_transfer_client.submit_transfer.call_args.args[0]
    assert submitted["DATA"][0]["source_path"] == "/workflow-repos-assets/test-repo/dev/abc123"


@pytest.mark.parametrize("changed", [False, True])
def test_enqueue_rechecks_checkout_after_preparation(test_db, repo_status_factory, changed):
    from app.schemas.backend import RepositoryTransferMode
    from tests.datagen import AppUserFactory, WorkflowRunFactory

    repo = repo_status_factory(status="completed")
    run = WorkflowRunFactory.create_sync(
        workflow=repo.workflow, backend=repo.compute_backend, owner=AppUserFactory.create_sync()
    )
    job = QueuedJobFactory.build(workflow=repo.workflow, workflow_run=run)
    if changed:
        with pytest.raises(ValueError, match="changed during preparation"):
            job.record_repository_preparation(
                test_db, RepositoryTransferMode.GLOBUS, "/old/checkout", False
            )
    else:
        job.record_repository_preparation(
            test_db, RepositoryTransferMode.GLOBUS, repo.location, False
        )
        assert job.repository_ready
