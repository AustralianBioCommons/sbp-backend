"""Coverage tests for job utility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.db.models.core import AppUser, RunMetric, RunOutput, S3Object, Workflow, WorkflowRun
from app.services import job_utils, results_utils


class _Result:
    def __init__(self, all_value=None, scalar_value=None):
        self._all = all_value or []
        self._scalar = scalar_value

    def all(self):
        return self._all

    def scalar_one_or_none(self):
        return self._scalar


class _DB:
    def __init__(self, all_rows=None, scalar=None):
        self._all_rows = all_rows or []
        self._scalar = scalar
        self.added = None
        self.committed = False

    def execute(self, *_args, **_kwargs):
        return _Result(all_value=self._all_rows, scalar_value=self._scalar)

    def add(self, obj):
        self.added = obj

    def commit(self):
        self.committed = True


def test_coerce_and_extract_helpers():
    payload = {"workflow": {"status": "RUNNING"}}
    assert job_utils.coerce_workflow_payload(payload) == payload["workflow"]
    assert job_utils.extract_pipeline_status(payload) == "RUNNING"


def test_parse_submit_datetime_invalid_returns_none():
    assert job_utils.parse_submit_datetime({"workflow": {"submit": "bad"}}) is None


def test_get_owned_run_ids_returns_only_current_user_runs(test_db):
    """Test that get_owned_run_ids returns only runs owned by the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add(user1)
    test_db.add(user2)
    test_db.commit()

    # Create runs for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    run2_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-2",
        work_dir="workdir-1002",
    )

    # Create runs for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    run2_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-2",
        work_dir="workdir-2002",
    )

    test_db.add_all([run1_user1, run2_user1, run1_user2, run2_user2])
    test_db.commit()

    # Get run IDs for user1 - should only return user1's runs
    user1_runs = job_utils.get_owned_run_ids(test_db, user1.id)
    assert user1_runs == {"run-user1-1", "run-user1-2"}
    assert "run-user2-1" not in user1_runs
    assert "run-user2-2" not in user1_runs

    # Get run IDs for user2 - should only return user2's runs
    user2_runs = job_utils.get_owned_run_ids(test_db, user2.id)
    assert user2_runs == {"run-user2-1", "run-user2-2"}
    assert "run-user1-1" not in user2_runs
    assert "run-user1-2" not in user2_runs


def test_get_score_by_seqera_run_id_returns_only_current_user_runs(test_db):
    """Test that get_score_by_seqera_run_id returns only scores for the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add_all([user1, user2])
    test_db.commit()

    # Create runs with metrics for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    run2_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-2",
        work_dir="workdir-1002",
    )
    test_db.add_all([run1_user1, run2_user1])
    test_db.commit()

    # Add metrics
    metric1 = RunMetric(run_id=run1_user1.id, max_score=0.9123)
    metric2 = RunMetric(run_id=run2_user1.id, max_score=None)
    test_db.add_all([metric1, metric2])

    # Create runs with metrics for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    test_db.add(run1_user2)
    test_db.commit()

    metric3 = RunMetric(run_id=run1_user2.id, max_score=0.5555)
    test_db.add(metric3)
    test_db.commit()

    # Get scores for user1
    user1_scores = job_utils.get_score_by_seqera_run_id(test_db, user1.id)
    assert user1_scores == {"run-user1-1": 0.91}  # Numeric(8, 2) precision
    assert "run-user2-1" not in user1_scores

    # Get scores for user2
    user2_scores = job_utils.get_score_by_seqera_run_id(test_db, user2.id)
    assert user2_scores == {"run-user2-1": 0.56}  # Numeric(8, 2) precision
    assert "run-user1-1" not in user2_scores


def test_get_workflow_type_by_seqera_run_id_returns_only_current_user_runs(test_db):
    """Test that get_workflow_type_by_seqera_run_id returns only workflow types for the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add_all([user1, user2])
    test_db.commit()

    # Create workflows
    workflow1 = Workflow(
        id=uuid4(),
        name="BindCraft",
        description="Binding workflow",
    )
    workflow2 = Workflow(
        id=uuid4(),
        name="OtherWorkflow",
        description="Other workflow",
    )
    test_db.add_all([workflow1, workflow2])
    test_db.commit()

    # Create runs for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        workflow_id=workflow1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    test_db.add(run1_user1)

    # Create runs for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        workflow_id=workflow2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    test_db.add(run1_user2)
    test_db.commit()

    # Get workflow types for user1
    user1_types = job_utils.get_workflow_type_by_seqera_run_id(test_db, user1.id)
    assert user1_types == {"run-user1-1": "BindCraft"}
    assert "run-user2-1" not in user1_types

    # Get workflow types for user2
    user2_types = job_utils.get_workflow_type_by_seqera_run_id(test_db, user2.id)
    assert user2_types == {"run-user2-1": "OtherWorkflow"}
    assert "run-user1-1" not in user2_types


@pytest.mark.asyncio
async def test_ensure_completed_bindcraft_score_branches():
    run = SimpleNamespace(id="rid", seqera_run_id="wf-1")

    # non-completed status
    assert await job_utils.ensure_completed_bindcraft_score(_DB(), run, "Failed") is None

    # existing score path
    db_existing = _DB(scalar=SimpleNamespace(max_score=0.9))
    assert await job_utils.ensure_completed_bindcraft_score(db_existing, run, "Completed") == 0.9

    # calculate + add path
    db_new = _DB(scalar=None)
    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.job_utils.calculate_csv_column_max",
            new_callable=AsyncMock,
            return_value=1.23,
        ),
    ):
        score = await job_utils.ensure_completed_bindcraft_score(db_new, run, "Completed")
    assert score == 1.0
    assert db_new.added is not None
    assert db_new.committed is True

    # calculate failure path
    db_fail = _DB(scalar=None)
    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.job_utils.calculate_csv_column_max",
            new_callable=AsyncMock,
            side_effect=ValueError("bad"),
        ),
    ):
        assert await job_utils.ensure_completed_bindcraft_score(db_fail, run, "Completed") is None


@pytest.mark.asyncio
async def test_ensure_completed_bindcraft_score_uses_run_outputs_file_key(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|score-user",
        name="Score User",
        email="score-user@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="seqera-123",
        work_dir="workdir-score-1",
    )
    output = S3Object(
        object_key="run-2026-01-29T01-25-32-i0cbrn/ranker/s1_final_design_stats.csv",
        uri="s3://bucket/run-2026-01-29T01-25-32-i0cbrn/ranker/s1_final_design_stats.csv",
    )
    run_output = RunOutput(run_id=run.id, s3_object_id=output.object_key)
    test_db.add_all([user, run, output, run_output])
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.job_utils.calculate_csv_column_max",
            new_callable=AsyncMock,
            return_value=0.88,
        ) as mocked_max,
    ):
        score = await job_utils.ensure_completed_bindcraft_score(test_db, run, "Completed")

    assert score == 0.88
    mocked_max.assert_awaited_once_with(
        file_key="run-2026-01-29T01-25-32-i0cbrn/ranker/s1_final_design_stats.csv",
        column_name="Average_i_pTM",
    )


@pytest.mark.asyncio
async def test_ensure_completed_bindcraft_score_uses_sample_name_final_design_stats(test_db):
    sample_id = "Anne_test"
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|sample-user",
        name="Sample User",
        email="sample-user@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="seqera-456",
        sample_id=sample_id,
        work_dir="workdir-score-2",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.job_utils.calculate_csv_column_max",
            new_callable=AsyncMock,
            return_value=0.91,
        ) as mocked_max,
    ):
        score = await job_utils.ensure_completed_bindcraft_score(test_db, run, "Completed")

    assert score == 0.91
    mocked_max.assert_awaited_once_with(
        file_key=f"{sample_id}/ranker/{sample_id}_final_design_stats.csv",
        column_name="Average_i_pTM",
    )


@pytest.mark.asyncio
async def test_sync_bindcraft_outputs_discovers_run_uuid_prefixed_snapshot_png(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|snapshot-user",
        name="Snapshot User",
        email="snapshot-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-snapshot-1",
        sample_id="sampleA",
        work_dir="workdir-snapshot-1",
    )
    test_db.add_all([user, run])
    test_db.commit()

    snapshot_key = f"{run_id}/bindcraft/sampleA_0_output/sampleA_preview.png"

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == f"{run_id}/bindcraft/sampleA_0_output/":
            return [
                {
                    "key": snapshot_key,
                    "size": 2048,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with patch(
        "app.services.results_utils.list_s3_files",
        new_callable=AsyncMock,
        side_effect=_list_side_effect,
    ):
        discovered = await results_utils.sync_bindcraft_outputs(test_db, run)

    assert snapshot_key in discovered
    persisted = test_db.get(S3Object, snapshot_key)
    assert persisted is not None
    assert persisted.uri.endswith(snapshot_key)
    link = (
        test_db.query(RunOutput).filter_by(run_id=run.id, s3_object_id=snapshot_key).one_or_none()
    )
    assert link is not None


@pytest.mark.asyncio
async def test_get_result_snapshot_downloads_returns_tracked_snapshots(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|snapshot-download-user",
        name="Snapshot Download User",
        email="snapshot-download-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-snapshot-download-1",
        sample_id="sampleB",
        work_dir="workdir-snapshot-download-1",
    )
    snapshot_keys = [
        f"{run_id}/bindcraft/sampleB_0_output/sampleB_preview.png",
        f"{run_id}/bindcraft/sampleB_0_output/sampleB_preview_2.png",
    ]
    snapshots = [S3Object(object_key=key, uri=f"s3://bucket/{key}") for key in snapshot_keys]
    test_db.add_all([user, run, *snapshots])
    test_db.add_all([RunOutput(run_id=run.id, s3_object_id=key) for key in snapshot_keys])
    test_db.commit()

    with (
        patch("app.services.results_utils.list_s3_files", new_callable=AsyncMock, return_value=[]),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ) as mocked_presign,
    ):
        result = await results_utils.get_result_snapshot_downloads(test_db, run)

    assert [item["category"] for item in result] == ["snapshot", "snapshot"]
    assert [item["key"] for item in result] == snapshot_keys
    assert mocked_presign.await_count == 2


@pytest.mark.asyncio
async def test_get_result_snapshot_downloads_discovers_snapshot_from_s3(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|snapshot-discovery-user",
        name="Snapshot Discovery User",
        email="snapshot-discovery-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-snapshot-download-2",
        sample_id="sampleC",
        work_dir="workdir-snapshot-download-2",
    )
    test_db.add_all([user, run])
    test_db.commit()

    snapshot_key = f"{run_id}/bindcraft/sampleC_0_output/sampleC_preview.png"

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == f"{run_id}/bindcraft/sampleC_0_output/":
            return [
                {
                    "key": snapshot_key,
                    "size": 2048,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await results_utils.get_result_snapshot_downloads(test_db, run)

    assert [item["key"] for item in result] == [snapshot_key]
    assert [item["category"] for item in result] == ["snapshot"]


@pytest.mark.asyncio
async def test_get_result_snapshot_downloads_returns_empty_when_missing(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|snapshot-missing-user",
        name="Snapshot Missing User",
        email="snapshot-missing-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-snapshot-download-3",
        sample_id="sampleD",
        work_dir="workdir-snapshot-download-3",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch("app.services.results_utils.list_s3_files", new_callable=AsyncMock, return_value=[]):
        result = await results_utils.get_result_snapshot_downloads(test_db, run)

    assert result == []


@pytest.mark.asyncio
async def test_get_result_report_download_returns_tracked_report(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|report-download-user",
        name="Report Download User",
        email="report-download-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-report-download-1",
        sample_id="sampleE",
        work_dir="workdir-report-download-1",
    )
    report_key = f"{run_id}/bindcraft/sampleE_0_output/Accepted/Animation/sampleE_report.html"
    report = S3Object(
        object_key=report_key,
        uri=f"s3://bucket/{report_key}",
    )
    test_db.add_all([user, run, report, RunOutput(run_id=run.id, s3_object_id=report_key)])
    test_db.commit()

    with (
        patch("app.services.results_utils.list_s3_files", new_callable=AsyncMock, return_value=[]),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ) as mocked_presign,
    ):
        result = await results_utils.get_result_report_download(test_db, run)

    assert result is not None
    assert result["category"] == "report"
    assert result["key"] == report_key
    assert result["url"] == f"https://signed.example/{report_key}"
    mocked_presign.assert_awaited_once_with(report_key)


@pytest.mark.asyncio
async def test_get_result_report_download_discovers_report_from_s3(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|report-discovery-user",
        name="Report Discovery User",
        email="report-discovery-user@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="seqera-report-download-2",
        sample_id="sampleF",
        work_dir="workdir-report-download-2",
    )
    test_db.add_all([user, run])
    test_db.commit()

    report_key = f"{run_id}/bindcraft/sampleF_0_output/Accepted/Animation/sampleF_report.html"

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == f"{run_id}/bindcraft/sampleF_0_output/Accepted/Animation/":
            return [
                {
                    "key": report_key,
                    "size": 1024,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with (
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await results_utils.get_result_report_download(test_db, run)

    assert result is not None
    assert result["key"] == report_key
    assert result["category"] == "report"


@pytest.mark.asyncio
async def test_get_result_report_download_falls_back_to_listing_when_sync_finds_nothing(test_db):
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=uuid4(),
        seqera_run_id="seqera-report-fallback-1",
        sample_id="sampleG",
        work_dir="workdir-report-fallback-1",
    )
    report_key = f"{run.id}/bindcraft/sampleG_0_output/Accepted/Animation/sampleG_report.html"

    with (
        patch("app.services.results_utils.sync_bindcraft_outputs", new=AsyncMock(return_value=[])),
        patch("app.services.results_utils._get_run_output_keys", return_value=[]),
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=lambda prefix: [{"key": report_key}]
            if prefix.endswith("Accepted/Animation/")
            else [],
        ),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await results_utils.get_result_report_download(test_db, run)

    assert result is not None
    assert result["key"] == report_key
    assert result["category"] == "report"


@pytest.mark.asyncio
async def test_get_result_snapshot_downloads_fall_back_to_listing_when_sync_finds_nothing(test_db):
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=uuid4(),
        seqera_run_id="seqera-snapshot-fallback-1",
        sample_id="sampleH",
        work_dir="workdir-snapshot-fallback-1",
    )
    snapshot_key = f"{run.id}/bindcraft/sampleH_0_output/sampleH_preview.png"

    with (
        patch("app.services.results_utils.sync_bindcraft_outputs", new=AsyncMock(return_value=[])),
        patch("app.services.results_utils._get_run_output_keys", return_value=[]),
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=lambda prefix: [{"key": snapshot_key}]
            if prefix.endswith("sampleH_0_output/")
            else [],
        ),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await results_utils.get_result_snapshot_downloads(test_db, run)

    assert [item["key"] for item in result] == [snapshot_key]
    assert [item["category"] for item in result] == ["snapshot"]
