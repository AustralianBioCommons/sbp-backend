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
    get_result_snapshots,
)
from app.services.s3 import S3ConfigurationError, S3ServiceError
from app.services.seqera_errors import SeqeraAPIError, SeqeraConfigurationError


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
async def test_get_result_setting_params_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-setting-missing",
        name="Results User Missing",
        email="results-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_setting_params("wf-setting-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


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
async def test_get_result_logs_handles_top_level_payload_and_seqera_defaults(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-logs-top-level",
        name="Results User Logs",
        email="results-logs@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-logs-top-level",
        work_dir="/tmp/wf-logs-top-level",
    )
    test_db.add_all([user, run])
    test_db.commit()

    payload = {
        "truncated": 1,
        "pending": 0,
        "message": None,
        "rewindToken": None,
        "forwardToken": None,
        "downloads": "not-a-list",
        "entries": None,
    }

    with patch(
        "app.routes.workflow.results.get_workflow_logs_raw",
        new=AsyncMock(return_value=payload),
    ):
        result = await get_result_logs("wf-logs-top-level", user.id, test_db)

    assert result.truncated is True
    assert result.pending is False
    assert result.message == ""
    assert result.rewindToken == ""
    assert result.forwardToken == ""
    assert result.downloads == []
    assert result.entries == []
    assert result.formattedEntries == []


@pytest.mark.asyncio
async def test_get_result_logs_maps_seqera_configuration_error_to_500(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-logs-config-error",
        name="Results User Logs Config",
        email="results-logs-config@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-logs-config-error",
        work_dir="/tmp/wf-logs-config-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_workflow_logs_raw",
        new=AsyncMock(side_effect=SeqeraConfigurationError("missing seqera config")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_logs("wf-logs-config-error", user.id, test_db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing seqera config"


@pytest.mark.asyncio
async def test_get_result_logs_maps_seqera_api_error_to_502(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-logs-api-error",
        name="Results User Logs API",
        email="results-logs-api@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-logs-api-error",
        work_dir="/tmp/wf-logs-api-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_workflow_logs_raw",
        new=AsyncMock(side_effect=SeqeraAPIError("seqera upstream failed")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_logs("wf-logs-api-error", user.id, test_db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "seqera upstream failed"


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
        S3Object(
            object_key=f"{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
            uri=f"s3://bucket/{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
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
    assert all(item.category != "snapshot" for item in result.downloads)
    assert (
        result.downloads[1].url
        == "https://signed.example/demo2/ranker/demo2_final_design_stats.csv"
    )
    assert mock_presign.await_count == 3


@pytest.mark.asyncio
async def test_get_result_downloads_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-downloads-missing",
        name="Results User Downloads Missing",
        email="results-downloads-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_downloads("wf-downloads-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_downloads_maps_s3_configuration_error_to_500(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-downloads-config-error",
        name="Results User Downloads Config",
        email="results-downloads-config@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-downloads-config-error",
        work_dir="/tmp/wf-downloads-config-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_output_downloads",
        new=AsyncMock(side_effect=S3ConfigurationError("missing s3 config")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_downloads("wf-downloads-config-error", user.id, test_db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_downloads_maps_s3_service_error_to_502(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-downloads-service-error",
        name="Results User Downloads Service",
        email="results-downloads-service@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-downloads-service-error",
        work_dir="/tmp/wf-downloads-service-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_output_downloads",
        new=AsyncMock(side_effect=S3ServiceError("s3 upstream failed")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_downloads("wf-downloads-service-error", user.id, test_db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


@pytest.mark.asyncio
async def test_get_result_snapshots_returns_presigned_links_for_tracked_outputs(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-snapshots-1",
        name="Results User Snapshots 1",
        email="results-snapshots1@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-snapshots-1",
        sample_id="demo2",
        work_dir="/tmp/wf-snapshots-1",
    )
    outputs = [
        S3Object(
            object_key=f"{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
            uri=f"s3://bucket/{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
        ),
        S3Object(
            object_key=f"{run.id}/bindcraft/demo2_0_output/demo2_preview_2.png",
            uri=f"s3://bucket/{run.id}/bindcraft/demo2_0_output/demo2_preview_2.png",
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
    ), patch(
        "app.services.job_utils.list_s3_files",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await get_result_snapshots("wf-snapshots-1", user.id, test_db)

    assert result.runId == "wf-snapshots-1"
    assert [item.category for item in result.snapshots] == ["snapshot", "snapshot"]
    assert [item.label for item in result.snapshots] == ["demo2_preview.png", "demo2_preview_2.png"]


@pytest.mark.asyncio
async def test_get_result_snapshots_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-snapshots-missing",
        name="Results User Snapshots Missing",
        email="results-snapshots-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_snapshots("wf-snapshots-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_snapshots_maps_s3_configuration_error_to_500(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-snapshots-config-error",
        name="Results User Snapshots Config",
        email="results-snapshots-config@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-snapshots-config-error",
        work_dir="/tmp/wf-snapshots-config-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_snapshot_downloads",
        new=AsyncMock(side_effect=S3ConfigurationError("missing s3 config")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_snapshots("wf-snapshots-config-error", user.id, test_db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_snapshots_maps_s3_service_error_to_502(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-snapshots-service-error",
        name="Results User Snapshots Service",
        email="results-snapshots-service@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-snapshots-service-error",
        work_dir="/tmp/wf-snapshots-service-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_snapshot_downloads",
        new=AsyncMock(side_effect=S3ServiceError("s3 upstream failed")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_snapshots("wf-snapshots-service-error", user.id, test_db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


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
        if prefix == f"{run_id}/bindcraft/s1_0_output/Accepted/Animation/":
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


@pytest.mark.asyncio
async def test_get_result_report_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-report-missing",
        name="Results User Report Missing",
        email="results-report-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_report("wf-report-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_report_maps_s3_configuration_error_to_500(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-report-config-error",
        name="Results User Report Config",
        email="results-report-config@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-report-config-error",
        work_dir="/tmp/wf-report-config-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(side_effect=S3ConfigurationError("missing s3 config")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_report("wf-report-config-error", user.id, test_db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_report_maps_s3_service_error_to_502(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-report-service-error",
        name="Results User Report Service",
        email="results-report-service@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-report-service-error",
        work_dir="/tmp/wf-report-service-error",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(side_effect=S3ServiceError("s3 upstream failed")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_report("wf-report-service-error", user.id, test_db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


@pytest.mark.asyncio
async def test_get_result_report_allows_missing_report_payload(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-report-none",
        name="Results User Report None",
        email="results-report-none@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-report-none",
        work_dir="/tmp/wf-report-none",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(return_value=None),
    ):
        result = await get_result_report("wf-report-none", user.id, test_db)

    assert result.runId == "wf-report-none"
    assert result.report is None
