"""Tests for staging a workflow's GitHub repo onto Gadi via S3 + Globus."""

from __future__ import annotations

import io
import json
import tarfile
from unittest.mock import MagicMock, patch

import httpx
import pytest
import requests
import respx

from app.config import GlobusSettings
from app.services.globus_errors import GlobusTransferError
from app.services.workflow_repo_staging import (
    RepoStagingError,
    build_repo_gadi_path,
    build_repo_s3_prefix,
    ensure_repo_staging_requested,
    parse_github_repo,
    poll_repo_staging,
    resolve_latest_commit_sha,
    stage_pending_repo,
    sync_workflow_repo_staging,
)
from tests.datagen import QueuedJobFactory, WorkflowFactory


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


def _build_tarball_bytes(top_dir: str, files: dict[str, str]) -> bytes:
    """Build an in-memory .tar.gz matching GitHub's tarball layout: one
    top-level "<owner>-<repo>-<short-sha>/" directory wrapping the checkout."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative_path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top_dir}/{relative_path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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


def test_build_repo_s3_prefix():
    assert (
        build_repo_s3_prefix("nf-core", "proteinfold", "abc123")
        == "workflow-repos/nf-core-proteinfold/abc123"
    )


def test_build_repo_gadi_path(globus_settings):
    path = build_repo_gadi_path("nf-core", "proteinfold", "abc123", globus_settings=globus_settings)
    assert path == "/test/workflow_repos/nf-core-proteinfold/abc123.git"


# ============================================================================
# resolve_latest_commit_sha
# ============================================================================


@respx.mock
def test_resolve_latest_commit_sha_success():
    respx.get("https://api.github.com/repos/nf-core/proteinfold/commits/dev").mock(
        return_value=httpx.Response(200, json={"sha": "abc123"})
    )
    assert resolve_latest_commit_sha("https://github.com/nf-core/proteinfold", "dev") == "abc123"


@respx.mock
def test_resolve_latest_commit_sha_http_error_raises():
    respx.get("https://api.github.com/repos/nf-core/proteinfold/commits/dev").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(RepoStagingError, match="Failed to resolve commit"):
        resolve_latest_commit_sha("https://github.com/nf-core/proteinfold", "dev")


@respx.mock
def test_resolve_latest_commit_sha_missing_sha_raises():
    respx.get("https://api.github.com/repos/nf-core/proteinfold/commits/dev").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(RepoStagingError, match="no commit sha"):
        resolve_latest_commit_sha("https://github.com/nf-core/proteinfold", "dev")


# ============================================================================
# ensure_repo_staging_requested
# ============================================================================


@respx.mock
def test_ensure_repo_staging_requested_marks_pending_on_cache_miss(
    test_db, persistent_models, mock_settings
):
    respx.get("https://api.github.com/repos/test/repo/commits/dev").mock(
        return_value=httpx.Response(200, json={"sha": "newsha"})
    )
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha=None,
        repo_staging_status=None,
    )

    gadi_path = ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert gadi_path == workflow.repo_gadi_path
    assert workflow.repo_staged_commit_sha == "newsha"
    assert workflow.repo_staging_status == "pending"


@respx.mock
def test_ensure_repo_staging_requested_resets_on_commit_change(
    test_db, persistent_models, mock_settings
):
    respx.get("https://api.github.com/repos/test/repo/commits/dev").mock(
        return_value=httpx.Response(200, json={"sha": "newsha"})
    )
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="oldsha",
        repo_staging_status="completed",
        repo_gadi_path="/test/workflow_repos/test-repo/oldsha",
        repo_staging_transfer_id="old-task-id",
        repo_staging_error_message=None,
    )

    ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staged_commit_sha == "newsha"
    assert workflow.repo_staging_status == "pending"
    assert workflow.repo_staging_transfer_id is None


@respx.mock
def test_ensure_repo_staging_requested_reuses_cache_hit(test_db, persistent_models, mock_settings):
    """Same commit, already completed - must not reset back to pending (that
    would re-trigger staging for no reason)."""
    respx.get("https://api.github.com/repos/test/repo/commits/dev").mock(
        return_value=httpx.Response(200, json={"sha": "samesha"})
    )
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="samesha",
        repo_staging_status="completed",
        repo_gadi_path="/test/workflow_repos/test-repo/samesha.git",
    )

    gadi_path = ensure_repo_staging_requested(test_db, workflow, settings=mock_settings)

    assert gadi_path == "/test/workflow_repos/test-repo/samesha.git"
    assert workflow.repo_staging_status == "completed"


@respx.mock
def test_ensure_repo_staging_requested_retries_after_failure(
    test_db, persistent_models, mock_settings
):
    """Same commit but previously failed - must re-request staging, not treat
    the failure as a permanent cache entry."""
    respx.get("https://api.github.com/repos/test/repo/commits/dev").mock(
        return_value=httpx.Response(200, json={"sha": "samesha"})
    )
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        default_revision="dev",
        repo_staged_commit_sha="samesha",
        repo_staging_status="failed",
        repo_staging_error_message="boom",
    )

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
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/abc123",
        repo_staging_transfer_id=None,
    )

    with patch(
        "app.services.workflow_repo_staging._download_and_upload_repo_tarball"
    ) as mock_download:
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    mock_download.assert_called_once()
    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["source_endpoint"] == "test-s3-collection-id"
    assert submitted["destination_endpoint"] == "test-gadi-collection-id"
    # NOT "/test/workflow_repos/test-repo/abc123" - that's the absolute path,
    # which would double-nest under the "/test" collection root on Gadi.
    assert submitted["DATA"][0]["destination_path"] == "/workflow_repos/test-repo/abc123"
    assert submitted["DATA"][0]["source_path"] == "/workflow-repos/test-repo/abc123"
    assert submitted["DATA"][0]["recursive"] is True

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
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
    )

    with patch(
        "app.services.workflow_repo_staging._download_and_upload_repo_tarball",
        side_effect=RepoStagingError("download failed"),
    ):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "failed"
    assert "download failed" in workflow.repo_staging_error_message
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
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/abc123",
    )

    with patch("app.services.workflow_repo_staging._download_and_upload_repo_tarball"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    assert workflow.repo_staging_status == "failed"
    assert "UNKNOWN_SCOPE_ERROR" in workflow.repo_staging_error_message


def test_stage_pending_repo_reuses_existing_submission_id(
    test_db, persistent_models, mock_transfer_client, mock_settings
):
    mock_transfer_client.submit_transfer.return_value = {"task_id": "task-1"}
    workflow = WorkflowFactory.create_sync(
        repo_url="https://github.com/test/repo",
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/abc123",
        repo_staging_transfer_id="already-committed-sub-id",
    )

    with patch("app.services.workflow_repo_staging._download_and_upload_repo_tarball"):
        stage_pending_repo(test_db, workflow, settings=mock_settings)

    mock_transfer_client.get_submission_id.assert_not_called()
    submitted = mock_transfer_client.submit_transfer.call_args[0][0]
    assert submitted["submission_id"] == "already-committed-sub-id"


# ============================================================================
# _download_and_upload_repo_tarball
# ============================================================================


@respx.mock
def test_download_and_upload_repo_tarball_uploads_each_file(mock_settings):
    from app.services.workflow_repo_staging import _download_and_upload_repo_tarball

    tarball_bytes = _build_tarball_bytes(
        "test-repo-abc123",
        {"main.nf": "process {}", "nextflow.config": "params {}"},
    )
    respx.get("https://api.github.com/repos/test/repo/tarball/abc123").mock(
        return_value=httpx.Response(200, content=tarball_bytes)
    )
    mock_s3_client = MagicMock()

    with patch(
        "app.services.workflow_repo_staging.get_s3_client", return_value=mock_s3_client
    ):
        _download_and_upload_repo_tarball(
            "test", "repo", "abc123", "workflow-repos/test-repo/abc123", settings=mock_settings
        )

    uploaded_keys = {call.args[2] for call in mock_s3_client.upload_file.call_args_list}
    assert uploaded_keys == {
        "workflow-repos/test-repo/abc123/main.nf",
        "workflow-repos/test-repo/abc123/nextflow.config",
    }


@respx.mock
def test_download_and_upload_repo_tarball_download_failure_raises(mock_settings):
    from app.services.workflow_repo_staging import _download_and_upload_repo_tarball

    respx.get("https://api.github.com/repos/test/repo/tarball/abc123").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(RepoStagingError, match="Failed to download tarball"):
        _download_and_upload_repo_tarball(
            "test", "repo", "abc123", "workflow-repos/test-repo/abc123", settings=mock_settings
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
        repo_staged_commit_sha="abc123",
        repo_staging_status="pending",
        repo_gadi_path="/test/workflow_repos/test-repo/abc123",
        repo_staging_transfer_id=None,
    )

    with patch("app.services.workflow_repo_staging._download_and_upload_repo_tarball"):
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
