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
from app.services.globus_errors import GlobusTransferError
from app.services.workflow_repo_staging import (
    RepoStagingError,
    build_repo_gadi_path,
    ensure_repo_staging_requested,
    parse_github_repo,
    poll_repo_staging,
    resolve_latest_commit_sha,
    stage_pending_repo,
    sync_workflow_repo_staging,
)
from tests.datagen import QueuedJobFactory, WorkflowFactory


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
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return str(repo_dir), commit_sha


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


def test_build_repo_gadi_path(globus_settings):
    path = build_repo_gadi_path(
        "nf-core", "proteinfold", "dev", "abc123", globus_settings=globus_settings
    )
    assert path == "/test/workflow_repos/proteinfold/dev/abc123/nf-core/proteinfold"


def test_build_repo_gadi_path_distinguishes_revisions_at_same_commit(globus_settings):
    """Two revisions resolving to the same commit must not collide on one
    checkout path - each name needs its own staged copy with its own ref."""
    main_path = build_repo_gadi_path(
        "nf-core", "proteinfold", "main", "abc123", globus_settings=globus_settings
    )
    staging_path = build_repo_gadi_path(
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
# ensure_repo_staging_requested
# ============================================================================


def test_ensure_repo_staging_requested_marks_pending_on_cache_miss(
    test_db, persistent_models, mock_settings
):
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha=None,
        repo_staging_status=None,
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="newsha"),
    ):
        locations = ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert locations.gadi_path == workflow.repo_gadi_path
    assert locations.assets_gadi_path == "/test/workflow_repos/repo/dev/newsha/test/repo"
    assert workflow.repo_staged_commit_sha == "newsha"
    assert workflow.repo_staging_status == "pending"


def test_ensure_repo_staging_requested_resets_on_commit_change(
    test_db, persistent_models, mock_settings
):
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="oldsha",
        repo_staging_status="completed",
        repo_gadi_path="/test/workflow_repos/test-repo/dev/oldsha.git",
        repo_staging_transfer_id="old-task-id",
        repo_staging_error_message=None,
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="newsha"),
    ):
        ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staged_commit_sha == "newsha"
    assert workflow.repo_staging_status == "pending"
    assert workflow.repo_staging_transfer_id is None


def test_ensure_repo_staging_requested_reuses_cache_hit(test_db, persistent_models, mock_settings):
    """Same commit, already completed - must not reset back to pending (that
    would re-trigger staging for no reason)."""
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="samesha",
        repo_staging_status="completed",
        repo_gadi_path="/test/workflow_repos/repo/dev/samesha/test/repo",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        locations = ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert locations.gadi_path == "/test/workflow_repos/repo/dev/samesha/test/repo"
    assert locations.assets_gadi_path == "/test/workflow_repos/repo/dev/samesha/test/repo"
    assert workflow.repo_staging_status == "completed"


def test_ensure_repo_staging_requested_restages_on_revision_change_same_commit(
    test_db, persistent_models, mock_settings
):
    """Regression test: a freshly-cut branch ("staging") can resolve to the
    exact same commit as the previously staged revision ("main"). Comparing
    commit_sha alone would wrongly call this "up to date" and reuse a
    checkout whose only ref is named "main", so a Nextflow launch asking for
    revision "staging" would fail with `invalid object name 'staging'`."""
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="staging",
        repo_staged_commit_sha="samesha",
        repo_staging_status="completed",
        repo_gadi_path="/test/workflow_repos/repo/main/samesha/test/repo",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        locations = ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert locations.gadi_path == "/test/workflow_repos/repo/staging/samesha/test/repo"
    assert workflow.repo_gadi_path == "/test/workflow_repos/repo/staging/samesha/test/repo"
    assert workflow.repo_staging_status == "pending"


def test_ensure_repo_staging_requested_retries_after_failure(
    test_db, persistent_models, mock_settings
):
    """Same commit but previously failed - must re-request staging, not treat
    the failure as a permanent cache entry."""
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="samesha",
        repo_staging_status="failed",
        repo_staging_error_message="boom",
    )

    with patch(
        "app.services.workflow_repo_staging._get_github_client",
        return_value=_mock_github_client(sha="samesha"),
    ):
        ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "pending"
    assert workflow.repo_staging_error_message is None


# ============================================================================
# stage_pending_repo
# ============================================================================


def test_stage_pending_repo_uses_collection_relative_destination_path(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    """Regression test: add_item must receive the Gadi path relative to the
    collection root, not the raw absolute Gadi filesystem path - sending the
    absolute path double-nests it under the collection root on the real
    filesystem (confirmed in production: the staged repo was unreachable at
    its expected path)."""
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}

    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/dev/abc123",
        repo_staging_transfer_id=None,
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

    assert workflow.repo_staging_status == "in_progress"
    assert workflow.repo_staging_transfer_id == "task-1"


def test_stage_pending_repo_no_commit_sha_raises(test_db, persistent_models, mock_settings):
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        repo_staged_commit_sha=None,
    )
    with pytest.raises(RepoStagingError, match="no commit sha"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)


def test_stage_pending_repo_download_failure_marks_failed(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
    )

    with patch(
        "app.services.workflow_repo_staging._clone_and_upload_repo",
        side_effect=RepoStagingError("clone failed"),
    ):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "failed"
    assert "clone failed" in workflow.repo_staging_error_message
    mock_transfer_client.submit_transfer.assert_not_called()


def test_stage_pending_repo_submission_api_error_marks_failed(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.side_effect = _globus_api_error(
        400, {"code": "UNKNOWN_SCOPE_ERROR", "message": "bad scope"}
    )
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/dev/abc123",
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "failed"
    assert "UNKNOWN_SCOPE_ERROR" in workflow.repo_staging_error_message


def test_stage_pending_repo_reuses_existing_submission_id(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/dev/abc123",
        repo_staging_transfer_id="already-committed-sub-id",
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


def test_poll_repo_staging_no_transfer_id_raises(test_db, persistent_models):
    workflow = WorkflowFactory.create_sync(repo_staging_transfer_id=None)
    with pytest.raises(GlobusTransferError, match="no transfer_id"):
        poll_repo_staging(test_db, workflow)


def test_poll_repo_staging_succeeded_marks_completed_and_promotes_queued_jobs(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    workflow = WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )
    QueuedJobFactory.create_sync(workflow=workflow, status="staging")

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "completed"


def test_poll_repo_staging_failed_records_fatal_error(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.return_value = {
        "status": "FAILED",
        "fatal_error": {"description": "permission denied"},
    }
    workflow = WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "failed"
    assert workflow.repo_staging_error_message == "permission denied"


def test_poll_repo_staging_active_leaves_status_unchanged(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.return_value = {"status": "ACTIVE"}
    workflow = WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )

    poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "in_progress"


def test_poll_repo_staging_api_error_records_message_without_changing_status(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.side_effect = _globus_api_error(
        401, {"code": "AuthenticationFailed", "message": "token expired"}
    )
    workflow = WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )

    with pytest.raises(GlobusTransferError, match="Failed to poll"):
        poll_repo_staging(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "in_progress"
    assert "Poll failed" in workflow.repo_staging_error_message


# ============================================================================
# sync_workflow_repo_staging
# ============================================================================


def test_sync_workflow_repo_staging_submits_pending(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_submission_id.return_value = {"value": "sub-1"}
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}
    WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/abc123",
        repo_staging_transfer_id=None,
    )

    with patch("app.services.workflow_repo_staging._clone_and_upload_repo"):
        result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.submitted == 1


def test_sync_workflow_repo_staging_polls_in_progress(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.return_value = {"status": "SUCCEEDED"}
    WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.completed == 1


def test_sync_workflow_repo_staging_ignores_workflows_not_staging(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    WorkflowFactory.create_sync(repo_staging_status="completed")
    WorkflowFactory.create_sync(repo_staging_status=None)

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 0
    mock_transfer_client.get_task.assert_not_called()


def test_sync_workflow_repo_staging_continues_after_unexpected_error(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.get_task.side_effect = RuntimeError("boom")
    WorkflowFactory.create_sync(
        repo_staging_transfer_id="task-1", repo_staging_status="in_progress"
    )

    result = sync_workflow_repo_staging(test_db, settings=mock_settings)

    assert result.checked == 1
    assert result.errored == 1
