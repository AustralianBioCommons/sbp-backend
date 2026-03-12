"""Tests for results routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.models.core import AppUser, RunMetric, RunOutput, S3Object, WorkflowRun
from app.routes.workflow.results import (
    get_result_downloads,
    get_result_logs,
    get_result_report,
    get_result_setting_params,
)


@pytest.mark.asyncio
async def test_get_result_setting_params_uses_stored_form_data(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user",
        name="Results User",
        email="results@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-1",
        submitted_form_data={
            "id": "s1",
            "binder_name": "PDL1",
            "number_of_final_designs": 100,
        },
        sample_id="s1",
        binder_name="PDL1",
        work_dir="/tmp/wf-1",
    )
    test_db.add_all([user, run])
    test_db.add(RunMetric(run_id=run.id, final_design_count=100))
    test_db.commit()

    result = await get_result_setting_params("wf-1", user.id, test_db)

    assert result.runId == "wf-1"
    assert result.settingParams == {
        "id": "s1",
        "binder_name": "PDL1",
        "number_of_final_designs": 100,
    }


@pytest.mark.asyncio
async def test_get_result_setting_params_falls_back_to_local_fields(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-2",
        name="Results User 2",
        email="results2@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-2",
        submitted_form_data=None,
        sample_id="s2",
        binder_name="PDL2",
        work_dir="/tmp/wf-2",
    )
    test_db.add_all([user, run])
    test_db.add(RunMetric(run_id=run.id, final_design_count=25))
    test_db.commit()

    result = await get_result_setting_params("wf-2", user.id, test_db)

    assert result.runId == "wf-2"
    assert result.settingParams == {
        "id": "s2",
        "binder_name": "PDL2",
        "number_of_final_designs": 25,
        "_source": "fallback_local",
        "_warning": "submitted_form_data_missing",
    }


@pytest.mark.asyncio
async def test_get_result_logs_returns_formatted_entries(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-3",
        name="Results User 3",
        email="results3@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-logs-1",
        work_dir="/tmp/wf-logs-1",
    )
    test_db.add_all([user, run])
    test_db.commit()

    payload = {
        "log": {
            "truncated": False,
            "pending": False,
            "message": "Logs retrieved",
            "rewindToken": "rewind-1",
            "forwardToken": "forward-1",
            "downloads": [{"label": "raw", "url": "https://example.test/logs.txt"}],
            "entries": [
                "2026-03-10T10:00:00Z INFO Starting workflow",
                "  \u001b[0;34mworkDir                   : \u001b[0;32m/scratch/yz52/sbp/workdir\u001b[0m",
            ],
        }
    }

    with patch(
        "app.routes.workflow.results.get_workflow_logs_raw",
        new=AsyncMock(return_value=payload),
    ):
        result = await get_result_logs("wf-logs-1", user.id, test_db)

    assert result.runId == "wf-logs-1"
    assert result.entries == payload["log"]["entries"]
    assert result.message == "Logs retrieved"
    assert len(result.formattedEntries) == 2
    assert result.formattedEntries[0].timestamp == "2026-03-10T10:00:00Z"
    assert result.formattedEntries[0].level == "INFO"
    assert result.formattedEntries[0].message == "INFO Starting workflow"
    assert (
        result.formattedEntries[1].message
        == "workDir                   : /scratch/yz52/sbp/workdir"
    )


@pytest.mark.asyncio
async def test_get_result_logs_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-4",
        name="Results User 4",
        email="results4@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_logs("wf-logs-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_downloads_returns_presigned_links_for_tracked_outputs(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-5",
        name="Results User 5",
        email="results5@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-downloads-1",
        sample_id="demo2",
        work_dir="/tmp/wf-downloads-1",
    )
    outputs = [
        S3Object(
            object_key="demo2/ranker/demo2_final_design_stats.csv",
            uri="s3://bucket/demo2/ranker/demo2_final_design_stats.csv",
        ),
        S3Object(
            object_key="demo2/ranker/demo2_Ranked/1_PDL1_model1.pdb",
            uri="s3://bucket/demo2/ranker/demo2_Ranked/1_PDL1_model1.pdb",
        ),
        S3Object(
            object_key="demo2/Accepted/Animation/PDL1_l100_s975117.html",
            uri="s3://bucket/demo2/Accepted/Animation/PDL1_l100_s975117.html",
        ),
    ]
    test_db.add_all([user, run, *outputs])
    test_db.commit()
    test_db.add_all([RunOutput(run_id=run.id, s3_object_id=item.object_key) for item in outputs])
    test_db.commit()

    with patch(
        "app.services.job_utils.generate_presigned_url",
        new_callable=AsyncMock,
        side_effect=lambda key: f"https://signed.example/{key}",
    ) as mock_presign, patch(
        "app.services.job_utils.list_s3_files",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await get_result_downloads("wf-downloads-1", user.id, test_db)

    assert result.runId == "wf-downloads-1"
    assert [item.category for item in result.downloads] == ["report", "stats_csv", "pdb"]
    assert [item.label for item in result.downloads] == [
        "PDL1_l100_s975117.html",
        "demo2_final_design_stats.csv",
        "1_PDL1_model1.pdb",
    ]
    assert (
        result.downloads[1].url
        == "https://signed.example/demo2/ranker/demo2_final_design_stats.csv"
    )
    assert mock_presign.await_count == 3


@pytest.mark.asyncio
async def test_get_result_downloads_falls_back_to_bindcraft_animation_prefix(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-6",
        name="Results User 6",
        email="results6@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-downloads-2",
        sample_id="demo2",
        work_dir="/tmp/wf-downloads-2",
    )
    test_db.add_all([user, run])
    test_db.commit()

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == "bindcraft/demo2_0_output/Accepted/Animation/":
            return [
                {
                    "key": "bindcraft/demo2_0_output/Accepted/Animation/PDL1_l100_s975117.html",
                    "size": 123,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with (
        patch(
            "app.services.job_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ) as mock_list,
        patch(
            "app.services.job_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await get_result_downloads("wf-downloads-2", user.id, test_db)

    assert len(result.downloads) == 1
    assert result.downloads[0].category == "report"
    assert result.downloads[0].key == (
        "bindcraft/demo2_0_output/Accepted/Animation/PDL1_l100_s975117.html"
    )
    assert mock_list.await_count >= 1


@pytest.mark.asyncio
async def test_get_result_report_returns_single_presigned_html_for_tracked_output(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-7",
        name="Results User 7",
        email="results7@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-report-1",
        sample_id="demo2",
        work_dir="/tmp/wf-report-1",
    )
    report = S3Object(
        object_key="demo2/Accepted/Animation/PDL1_l100_s975117.html",
        uri="s3://bucket/demo2/Accepted/Animation/PDL1_l100_s975117.html",
    )
    test_db.add_all([user, run, report])
    test_db.commit()
    test_db.add(RunOutput(run_id=run.id, s3_object_id=report.object_key))
    test_db.commit()

    with patch(
        "app.services.job_utils.generate_presigned_url",
        new_callable=AsyncMock,
        side_effect=lambda key: f"https://signed.example/{key}",
    ) as mock_presign, patch(
        "app.services.job_utils.list_s3_files",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await get_result_report("wf-report-1", user.id, test_db)

    assert result.runId == "wf-report-1"
    assert result.report is not None
    assert result.report.category == "report"
    assert result.report.key == "demo2/Accepted/Animation/PDL1_l100_s975117.html"
    assert (
        result.report.url
        == "https://signed.example/demo2/Accepted/Animation/PDL1_l100_s975117.html"
    )
    mock_presign.assert_awaited_once_with("demo2/Accepted/Animation/PDL1_l100_s975117.html")


@pytest.mark.asyncio
async def test_get_result_report_falls_back_to_bindcraft_animation_prefix(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-8",
        name="Results User 8",
        email="results8@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-report-2",
        sample_id="demo2",
        work_dir="/tmp/wf-report-2",
    )
    test_db.add_all([user, run])
    test_db.commit()

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == "bindcraft/demo2_0_output/Accepted/Animation/":
            return [
                {
                    "key": "bindcraft/demo2_0_output/Accepted/Animation/PDL1_l70_s151467.html",
                    "size": 123,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with (
        patch(
            "app.services.job_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ),
        patch(
            "app.services.job_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await get_result_report("wf-report-2", user.id, test_db)

    assert result.report is not None
    assert result.report.key == (
        "bindcraft/demo2_0_output/Accepted/Animation/PDL1_l70_s151467.html"
    )


@pytest.mark.asyncio
async def test_get_result_report_syncs_run_uuid_prefixed_animation_output(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-9",
        name="Results User 9",
        email="results9@example.com",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        owner_user_id=user.id,
        seqera_run_id="wf-report-3",
        sample_id="s1",
        work_dir="/tmp/wf-report-3",
    )
    test_db.add_all([user, run])
    test_db.commit()

    real_key = f"{run_id}/bindcraft/s1_0_output/Accepted/Animation/PDL1_l79_s800698.html"

    def _list_side_effect(prefix: str, file_extension=None):
        if prefix == f"{run_id}/":
            return [
                {
                    "key": real_key,
                    "size": 123,
                    "last_modified": "2026-03-12T00:00:00Z",
                    "bucket": "test-bucket",
                }
            ]
        return []

    with (
        patch(
            "app.services.job_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ),
        patch(
            "app.services.job_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key: f"https://signed.example/{key}",
        ),
    ):
        result = await get_result_report("wf-report-3", user.id, test_db)

    assert result.report is not None
    assert result.report.key == real_key

    synced_output = test_db.get(S3Object, real_key)
    assert synced_output is not None
    assert synced_output.uri.endswith(real_key)

    synced_link = (
        test_db.query(RunOutput).filter_by(run_id=run.id, s3_object_id=real_key).one_or_none()
    )
    assert synced_link is not None
