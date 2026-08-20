"""Tests for results routes."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

from app.db.models.core import (
    AppUser,
    DataTransfer,
    RunMetric,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)
from app.routes.workflow.results import (
    get_result_download_all,
    get_result_downloads,
    get_result_file,
    get_result_logs,
    get_result_report,
    get_result_setting_params,
    get_result_snapshots,
)
from app.services.s3 import S3ConfigurationError, S3ServiceError
from app.services.seqera_errors import SeqeraAPIError
from tests.datagen import AppUserFactory, WorkflowFactory, WorkflowRunFactory


def _configure_bindcraft_run(run: WorkflowRun) -> None:
    run.workflow = Workflow(
        name="de-novo-design",
        repo_url="https://github.com/test/de-novo-design",
        default_revision="main",
        config_path="/config/de-novo-design.config",
    )
    run.submitted_form_data = {"mode": "bindcraft"}


def _make_run_output(run: WorkflowRun, object_key: str) -> RunOutput:
    """Build a RunOutput row linked to a throwaway DataTransfer for test fixtures."""
    transfer = DataTransfer(
        workflow_run=run,
        direction="output",
        provider="s3",
        source_location=f"/work/{object_key}",
        destination_location=f"s3://bucket/{object_key}",
        recursive=False,
    )
    return RunOutput(run_id=run.id, s3_object_id=object_key, data_transfer=transfer)


@pytest.mark.asyncio
async def test_get_result_setting_params_uses_stored_form_data(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user",
        name="Results User",
        email="results@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
    test_db.add(RunMetric(run=run, final_design_count=100))
    test_db.commit()

    result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert result.settingParams == {
        "id": "s1",
        "binder_name": "PDL1",
        "number_of_final_designs": 100,
    }


@pytest.mark.asyncio
async def test_get_result_setting_params_falls_back_to_local_fields(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-2",
        name="Results User 2",
        email="results2@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-2",
        submitted_form_data=None,
        sample_id="s2",
        binder_name="PDL2",
        work_dir="/tmp/wf-2",
    )
    test_db.add_all([user, run])
    test_db.add(RunMetric(run=run, final_design_count=25))
    test_db.commit()

    result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert result.settingParams == {
        "id": "s2",
        "binder_name": "PDL2",
        "number_of_final_designs": 25,
        "_source": "fallback_local",
        "_warning": "submitted_form_data_missing",
    }


@pytest.mark.asyncio
async def test_get_result_setting_params_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-setting-missing",
        name="Results User Missing",
        email="results-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_setting_params("wf-setting-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_setting_params_resolves_pdb_s3_uri_to_presigned_url(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-pdb-user",
        name="PDB User",
        email="pdb@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-pdb-1",
        submitted_form_data={
            "binder_name": "PDL1",
            "starting_pdb": "s3://my-bucket/uploads/target.pdb",
        },
        work_dir="/tmp/wf-pdb-1",
    )
    test_db.add_all([user, run])
    test_db.commit()

    presigned = "https://my-bucket.s3.amazonaws.com/uploads/target.pdb?X-Amz-Signature=test"
    with patch(
        "app.services.results_utils.generate_presigned_url",
        new=AsyncMock(return_value=presigned),
    ):
        result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.settingParams["binder_name"] == "PDL1"
    assert result.settingParams["starting_pdb"] == presigned


@pytest.mark.asyncio
async def test_get_result_setting_params_keeps_pdb_s3_uri_on_s3_error(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-pdb-err-user",
        name="PDB Err User",
        email="pdberr@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-pdb-err",
        submitted_form_data={
            "starting_pdb": "s3://my-bucket/uploads/target.pdb",
        },
        work_dir="/tmp/wf-pdb-err",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.services.results_utils.generate_presigned_url",
        new=AsyncMock(side_effect=S3ServiceError("presign failed")),
    ):
        result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.settingParams["starting_pdb"] == "s3://my-bucket/uploads/target.pdb"


@pytest.mark.asyncio
async def test_get_result_logs_returns_formatted_entries(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-3",
        name="Results User 3",
        email="results3@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
        result = await get_result_logs(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
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
async def test_get_result_logs_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-4",
        name="Results User 4",
        email="results4@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_logs("wf-logs-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_logs_handles_top_level_payload_and_seqera_defaults(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-user-logs-top-level",
        name="Results User Logs",
        email="results-logs@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
        result = await get_result_logs(str(run.id), user.id, test_db, mock_settings)

    assert result.truncated is True
    assert result.pending is False
    assert result.message == ""
    assert result.rewindToken == ""
    assert result.forwardToken == ""
    assert result.downloads == []
    assert result.entries == []
    assert result.formattedEntries == []


@pytest.mark.asyncio
async def test_get_result_logs_maps_seqera_api_error_to_502(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-logs-api-error",
        name="Results User Logs API",
        email="results-logs-api@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_logs(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "seqera upstream failed"


@pytest.mark.asyncio
async def test_get_result_downloads_returns_presigned_links_for_tracked_outputs(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-user-5",
        name="Results User 5",
        email="results5@example.com",
    )
    workflow = Workflow(
        name="de-novo-design",
        repo_url="https://github.com/test/de-novo-design",
        default_revision="main",
        config_path="/config/de-novo-design.config",
    )
    run = WorkflowRun(
        owner=user,
        workflow=workflow,
        submitted_form_data={"mode": "bindcraft"},
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
            object_key=f"{run.id}/generate/PDL1_l100_s975117.html",
            uri=f"s3://bucket/{run.id}/generate/PDL1_l100_s975117.html",
        ),
        S3Object(
            object_key=f"{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
            uri=f"s3://bucket/{run.id}/bindcraft/demo2_0_output/demo2_preview.png",
        ),
    ]
    test_db.add_all([user, run, workflow, *outputs])
    test_db.commit()
    test_db.add_all([_make_run_output(run, item.object_key) for item in outputs])
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key, **_kwargs: f"https://signed.example/{key}",
        ) as mock_presign,
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await get_result_downloads(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
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
async def test_get_result_downloads_returns_syncing_status_without_s3_lookup(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-downloads-syncing",
        name="Downloads Syncing",
        email="results-downloads-syncing@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-downloads-syncing",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
        work_dir="/tmp/wf-downloads-syncing",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_output_downloads",
        new=AsyncMock(),
    ) as get_downloads:
        result = await get_result_downloads(str(run.id), user.id, test_db, mock_settings)

    get_downloads.assert_not_awaited()
    assert result.runId == str(run.id)
    assert result.resultsSyncStatus == "syncing"
    assert result.downloads == []


@pytest.mark.asyncio
async def test_get_result_download_all_returns_valid_zip_file(
    test_db, persistent_models, mock_settings
):
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="de-novo-design")
    run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        tool="bindcraft",
        seqera_run_id="wf-download-all-1",
        run_name="download-all-run",
    )
    test_db.add_all([user, workflow, run])
    test_db.flush()

    output_contents = {
        f"{run.id}/generate/result.html": b"<html>report</html>",
        f"{run.id}/ranker/download-all_final_design_stats.csv": b"score\n0.9\n",
        f"{run.id}/ranker/download-all_Ranked/model.pdb": b"ATOM\n",
    }
    outputs = [S3Object(object_key=key, uri=f"s3://bucket/{key}") for key in output_contents]
    test_db.add_all(outputs)
    test_db.commit()
    test_db.add_all([_make_run_output(run, item.object_key) for item in outputs])
    test_db.commit()

    async def read_bytes(key: str, **_kwargs) -> bytes:
        return output_contents[key]

    with patch("app.services.results_utils.read_s3_bytes", new=AsyncMock(side_effect=read_bytes)):
        response = await get_result_download_all(str(run.id), user.id, test_db, mock_settings)

    body = b"".join([chunk async for chunk in response.body_iterator])
    returned_zip = BytesIO(body)

    assert response.media_type == "application/zip"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="results-download-all-run.zip"; '
        "filename*=UTF-8''results-download-all-run.zip"
    )
    with ZipFile(returned_zip) as zip_file:
        assert set(zip_file.namelist()) == {
            "report/result.html",
            "stats_csv/download-all_final_design_stats.csv",
            "pdb/model.pdb",
        }
        assert zip_file.read("report/result.html") == b"<html>report</html>"
        assert zip_file.read("stats_csv/download-all_final_design_stats.csv") == b"score\n0.9\n"
        assert zip_file.read("pdb/model.pdb") == b"ATOM\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected_outputs"),
    [
        (
            "boltz",
            [
                ("report", "T1024_boltz_report.html", "reports/T1024_boltz_report.html"),
                ("stats_csv", "confidence.tsv", "boltz/T1024/confidence.tsv"),
                ("pdb", "T1024.pdb", "boltz/top_ranked_structures/T1024.pdb"),
                ("alignment", "T1024.a3m", "mmseqs/T1024.a3m"),
            ],
        ),
        (
            "alphafold2",
            [
                ("report", "T1024_alphafold2_report.html", "reports/T1024_alphafold2_report.html"),
                (
                    "stats_csv",
                    "ranking.tsv",
                    "alphafold2/split_msa_prediction/T1024/ranking.tsv",
                ),
                (
                    "pdb",
                    "T1024.pdb",
                    "alphafold2/split_msa_prediction/top_ranked_structures/T1024.pdb",
                ),
            ],
        ),
        (
            "colabfold",
            [
                ("report", "T1024_colabfold_report.html", "reports/T1024_colabfold_report.html"),
                ("stats_csv", "scores.tsv", "colabfold/T1024/scores.tsv"),
                ("pdb", "T1024.pdb", "colabfold/top_ranked_structures/T1024.pdb"),
                ("alignment", "T1024.a3m", "mmseqs/T1024.a3m"),
            ],
        ),
    ],
)
async def test_get_result_downloads_returns_presigned_links_for_proteinfold_outputs(
    test_db, tool, expected_outputs, mock_settings
):
    user = AppUser(
        auth0_user_id=f"auth0|results-proteinfold-downloads-{tool}",
        name=f"Proteinfold Downloads {tool}",
        email=f"results-proteinfold-downloads-{tool}@example.com",
    )
    workflow = Workflow(
        name="single-prediction",
        repo_url="https://github.com/test/single-prediction",
        default_revision="main",
        config_path="/config/single-prediction.config",
    )
    run = WorkflowRun(
        owner=user,
        workflow=workflow,
        tool=tool,
        submitted_form_data={"tool": tool},
        seqera_run_id=f"wf-proteinfold-downloads-{tool}",
        sample_id="T1024",
        work_dir=f"/tmp/wf-proteinfold-downloads-{tool}",
    )
    test_db.add_all([user, workflow, run])
    test_db.commit()

    outputs = [
        S3Object(
            object_key=f"{run.id}/{relative_key}",
            uri=f"s3://bucket/{run.id}/{relative_key}",
        )
        for _, _, relative_key in expected_outputs
    ]
    test_db.add_all(outputs)
    test_db.commit()
    test_db.add_all([_make_run_output(run, item.object_key) for item in outputs])
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key, **_kwargs: f"https://signed.example/{key}",
        ) as mock_presign,
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_list_s3_files,
    ):
        result = await get_result_downloads(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert [item.category for item in result.downloads] == [
        category for category, _, _ in expected_outputs
    ]
    assert [item.label for item in result.downloads] == [label for _, label, _ in expected_outputs]
    assert [item.key for item in result.downloads] == [
        f"{run.id}/{relative_key}" for _, _, relative_key in expected_outputs
    ]
    assert [item.url for item in result.downloads] == [
        f"https://signed.example/{run.id}/{relative_key}" for _, _, relative_key in expected_outputs
    ]
    assert mock_presign.await_count == len(expected_outputs)
    mock_list_s3_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_result_downloads_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-downloads-missing",
        name="Results User Downloads Missing",
        email="results-downloads-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_downloads("wf-downloads-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_downloads_maps_s3_configuration_error_to_500(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-downloads-config-error",
        name="Results User Downloads Config",
        email="results-downloads-config@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_downloads(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_downloads_maps_s3_service_error_to_502(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-downloads-service-error",
        name="Results User Downloads Service",
        email="results-downloads-service@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_downloads(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


@pytest.mark.asyncio
async def test_get_result_snapshots_returns_presigned_links_for_tracked_outputs(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-user-snapshots-1",
        name="Results User Snapshots 1",
        email="results-snapshots1@example.com",
    )
    workflow = Workflow(
        name="de-novo-design",
        repo_url="https://github.com/test/de-novo-design",
        default_revision="main",
        config_path="/config/de-novo-design.config",
    )
    run = WorkflowRun(
        workflow=workflow,
        owner=user,
        submitted_form_data={"mode": "bindcraft"},
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
    test_db.add_all([user, run, workflow, *outputs])
    test_db.commit()
    test_db.add_all([_make_run_output(run, item.object_key) for item in outputs])
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key, **_kwargs: f"https://signed.example/{key}",
        ),
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await get_result_snapshots(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert [item.category for item in result.snapshots] == ["snapshot", "snapshot"]
    assert [item.label for item in result.snapshots] == ["demo2_preview.png", "demo2_preview_2.png"]


@pytest.mark.asyncio
async def test_get_result_snapshots_returns_syncing_status_without_s3_lookup(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-snapshots-syncing",
        name="Snapshots Syncing",
        email="results-snapshots-syncing@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-snapshots-syncing",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
        work_dir="/tmp/wf-snapshots-syncing",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_snapshot_downloads",
        new=AsyncMock(),
    ) as get_snapshots:
        result = await get_result_snapshots(str(run.id), user.id, test_db, mock_settings)

    get_snapshots.assert_not_awaited()
    assert result.runId == str(run.id)
    assert result.resultsSyncStatus == "syncing"
    assert result.snapshots == []


@pytest.mark.asyncio
async def test_get_result_snapshots_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-snapshots-missing",
        name="Results User Snapshots Missing",
        email="results-snapshots-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_snapshots("wf-snapshots-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_snapshots_maps_s3_configuration_error_to_500(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-snapshots-config-error",
        name="Results User Snapshots Config",
        email="results-snapshots-config@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_snapshots(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_snapshots_maps_s3_service_error_to_502(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-snapshots-service-error",
        name="Results User Snapshots Service",
        email="results-snapshots-service@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_snapshots(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


@pytest.mark.asyncio
async def test_get_result_report_returns_single_presigned_html_for_tracked_output(
    test_db, mock_settings
):
    user = AppUser(
        auth0_user_id="auth0|results-user-7",
        name="Results User 7",
        email="results7@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-report-1",
        sample_id="demo2",
        work_dir="/tmp/wf-report-1",
    )
    _configure_bindcraft_run(run)
    report_key = f"{run.id}/generate/PDL1_l100_s975117.html"
    report = S3Object(
        object_key=report_key,
        uri=f"s3://bucket/{report_key}",
    )
    test_db.add_all([user, run, report])
    test_db.commit()
    test_db.add(_make_run_output(run, report.object_key))
    test_db.commit()

    with (
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key, **_kwargs: f"https://signed.example/{key}",
        ) as mock_presign,
        patch(
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await get_result_report(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert result.report is not None
    assert result.report.category == "report"
    assert result.report.key == report_key
    assert result.report.url == f"https://signed.example/{report_key}"
    mock_presign.assert_awaited_once_with(
        report_key,
        response_content_type="text/html",
        response_content_disposition="inline",
        settings=mock_settings,
    )


@pytest.mark.asyncio
async def test_get_result_report_returns_syncing_status_without_s3_lookup(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-report-syncing",
        name="Report Syncing",
        email="results-report-syncing@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-report-syncing",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
        work_dir="/tmp/wf-report-syncing",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(),
    ) as get_report:
        result = await get_result_report(str(run.id), user.id, test_db, mock_settings)

    get_report.assert_not_awaited()
    assert result.runId == str(run.id)
    assert result.resultsSyncStatus == "syncing"
    assert result.report is None


@pytest.mark.asyncio
async def test_get_result_report_syncs_run_uuid_prefixed_animation_output(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-9",
        name="Results User 9",
        email="results9@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-report-3",
        sample_id="s1",
        work_dir="/tmp/wf-report-3",
    )
    _configure_bindcraft_run(run)
    test_db.add_all([user, run])
    test_db.commit()
    run_id = run.id

    real_key = f"{run_id}/generate/PDL1_l79_s800698.html"

    def _list_side_effect(prefix: str, file_extension=None, **_kwargs):
        if prefix == f"{run_id}/generate/":
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
            "app.services.results_utils.list_s3_files",
            new_callable=AsyncMock,
            side_effect=_list_side_effect,
        ),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new_callable=AsyncMock,
            side_effect=lambda key, **_kwargs: f"https://signed.example/{key}",
        ),
    ):
        result = await get_result_report(str(run.id), user.id, test_db, mock_settings)

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
async def test_get_result_report_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-report-missing",
        name="Results User Report Missing",
        email="results-report-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_report("wf-report-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_report_maps_multiple_reports_to_409(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-report-conflict",
        name="Results User Report Conflict",
        email="results-report-conflict@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-report-conflict",
        work_dir="/tmp/wf-report-conflict",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(side_effect=ValueError("Multiple report outputs found")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_report(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Multiple report outputs found"


@pytest.mark.asyncio
async def test_get_result_report_maps_s3_configuration_error_to_500(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-report-config-error",
        name="Results User Report Config",
        email="results-report-config@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_report(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "missing s3 config"


@pytest.mark.asyncio
async def test_get_result_report_maps_s3_service_error_to_502(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-report-service-error",
        name="Results User Report Service",
        email="results-report-service@example.com",
    )
    run = WorkflowRun(
        owner=user,
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
            await get_result_report(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream failed"


@pytest.mark.asyncio
async def test_get_result_report_allows_missing_report_payload(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|results-user-report-none",
        name="Results User Report None",
        email="results-report-none@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-report-none",
        work_dir="/tmp/wf-report-none",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_result_report_download",
        new=AsyncMock(return_value=None),
    ):
        result = await get_result_report(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert result.report is None


@pytest.mark.asyncio
async def test_get_result_setting_params_overlays_queued_job_payload(test_db, mock_settings):
    from app.db.models.core import Workflow
    from app.db.models.job_queue import QueuedJob

    user = AppUser(
        auth0_user_id="auth0|results-queued-job-user",
        name="Queued Job User",
        email="queued-job@example.com",
    )
    workflow = Workflow(
        name="de-novo-design-queued",
        repo_url="https://github.com/test/de-novo-design-queued",
        default_revision="main",
        config_path="/config/de-novo-design-queued.config",
    )
    run = WorkflowRun(
        owner=user,
        workflow=workflow,
        seqera_run_id="wf-queued-1",
        submitted_form_data={"binder_name": "PDL1"},
        work_dir="/tmp/wf-queued-1",
    )
    test_db.add_all([user, workflow, run])
    test_db.flush()
    job = QueuedJob(
        workflow_run_id=run.id,
        workflow_id=workflow.id,
        launch_payload={
            "paramsText": "binder_name: PDL1\nnum_designs: 5",
            "configProfiles": ["singularity", "gadi"],
        },
    )
    test_db.add(job)
    test_db.commit()

    result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.runId == str(run.id)
    assert result.settingParams["paramsText"] == {"binder_name": "PDL1", "num_designs": 5}
    assert result.settingParams["configProfiles"] == ["singularity", "gadi"]


@pytest.mark.asyncio
async def test_get_result_setting_params_queued_job_invalid_yaml_kept_as_string(
    test_db, mock_settings
):
    from app.db.models.core import Workflow
    from app.db.models.job_queue import QueuedJob

    user = AppUser(
        auth0_user_id="auth0|results-queued-job-user-2",
        name="Queued Job User 2",
        email="queued-job2@example.com",
    )
    workflow = Workflow(
        name="de-novo-design-queued-2",
        repo_url="https://github.com/test/de-novo-design-queued-2",
        default_revision="main",
        config_path="/config/de-novo-design-queued-2.config",
    )
    run = WorkflowRun(
        owner=user,
        workflow=workflow,
        seqera_run_id="wf-queued-2",
        submitted_form_data={"binder_name": "PDL1"},
        work_dir="/tmp/wf-queued-2",
    )
    test_db.add_all([user, workflow, run])
    test_db.flush()
    job = QueuedJob(
        workflow_run_id=run.id,
        workflow_id=workflow.id,
        launch_payload={"paramsText": "{\x00invalid yaml"},
    )
    test_db.add(job)
    test_db.commit()

    result = await get_result_setting_params(str(run.id), user.id, test_db, mock_settings)

    assert result.settingParams["paramsText"] == "{\x00invalid yaml"


@pytest.mark.asyncio
async def test_get_result_download_all_returns_404_for_missing_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|download-all-missing",
        name="Download All Missing",
        email="download-all-missing@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_download_all("wf-download-all-missing", user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_download_all_returns_404_while_results_sync(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|download-all-syncing",
        name="Download All Syncing",
        email="download-all-syncing@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-download-all-syncing",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
        work_dir="/tmp/wf-download-all-syncing",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_all_downloads_zipped",
        new=AsyncMock(),
    ) as get_zip:
        with pytest.raises(HTTPException) as exc_info:
            await get_result_download_all(str(run.id), user.id, test_db, mock_settings)

    get_zip.assert_not_awaited()
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Results are still syncing"


@pytest.mark.asyncio
async def test_get_result_download_all_maps_s3_configuration_error_to_500(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|download-all-config-err",
        name="Download All Config Err",
        email="download-all-config-err@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-download-all-config-err",
        work_dir="/tmp/wf-download-all-config-err",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_all_downloads_zipped",
        new=AsyncMock(side_effect=S3ConfigurationError("s3 config missing")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_download_all(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "s3 config missing"


@pytest.mark.asyncio
async def test_get_result_download_all_maps_s3_service_error_to_502(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|download-all-service-err",
        name="Download All Service Err",
        email="download-all-service-err@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-download-all-service-err",
        work_dir="/tmp/wf-download-all-service-err",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.get_all_downloads_zipped",
        new=AsyncMock(side_effect=S3ServiceError("s3 upstream error")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_download_all(str(run.id), user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream error"


# --- GET /{run_id}/file -------------------------------------------------------


def _make_boltz_prediction_run(test_db, suffix: str) -> tuple[AppUser, WorkflowRun, str, str]:
    """Create a single-prediction run with a structure and a PAE output tracked."""
    user = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync(name="single-prediction")
    run = WorkflowRunFactory.create_sync(
        owner=user,
        workflow=workflow,
        tool="boltz",
        seqera_run_id=f"wf-file-{suffix}",
        sample_id="T1024",
    )
    test_db.add_all([user, workflow, run])
    test_db.flush()

    structure_key = f"{run.id}/boltz/top_ranked_structures/T1024.cif"
    pae_key = f"{run.id}/boltz/T1024/paes/T1024_0_pae.tsv"
    outputs = [
        S3Object(object_key=key, uri=f"s3://bucket/{key}") for key in (structure_key, pae_key)
    ]
    test_db.add_all(outputs)
    test_db.commit()
    test_db.add_all([_make_run_output(run, item.object_key) for item in outputs])
    test_db.commit()

    return user, run, structure_key, pae_key


@pytest.mark.asyncio
async def test_get_result_file_returns_structure_as_text(test_db, persistent_models, mock_settings):
    user, run, structure_key, _ = _make_boltz_prediction_run(test_db, "structure")

    with patch(
        "app.services.results_utils.read_s3_bytes",
        new=AsyncMock(return_value=b"data_T1024\n"),
    ):
        response = await get_result_file(
            str(run.id), structure_key, user.id, test_db, mock_settings
        )

    assert response.body == b"data_T1024\n"
    assert response.media_type == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == 'inline; filename="T1024.cif"'


@pytest.mark.asyncio
async def test_get_result_file_returns_pae_matrix_as_text(
    test_db, persistent_models, mock_settings
):
    user, run, _, pae_key = _make_boltz_prediction_run(test_db, "pae")

    with patch(
        "app.services.results_utils.read_s3_bytes",
        new=AsyncMock(return_value=b"0\t1\n1\t0\n"),
    ):
        response = await get_result_file(str(run.id), pae_key, user.id, test_db, mock_settings)

    assert response.body == b"0\t1\n1\t0\n"
    assert response.media_type == "text/plain; charset=utf-8"


@pytest.mark.asyncio
async def test_get_result_file_returns_404_while_results_sync(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|file-syncing",
        name="File Syncing",
        email="file-syncing@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="wf-file-syncing",
        seqera_final_status="SUCCEEDED",
        sync_completed_at=None,
        work_dir="/tmp/wf-file-syncing",
    )
    test_db.add_all([user, run])
    test_db.commit()

    with patch(
        "app.routes.workflow.results.read_result_output_file",
        new=AsyncMock(),
    ) as read_file:
        with pytest.raises(HTTPException) as exc_info:
            await get_result_file(str(run.id), "run/output.cif", user.id, test_db, mock_settings)

    read_file.assert_not_awaited()
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Results are still syncing"


@pytest.mark.asyncio
async def test_get_result_file_rejects_a_key_the_run_does_not_own(
    test_db, persistent_models, mock_settings
):
    user, run, _, _ = _make_boltz_prediction_run(test_db, "foreign-key")

    with patch(
        "app.services.results_utils.list_workflow_outputs_from_s3",
        new=AsyncMock(return_value={}),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_file(
                str(run.id), "other-run/secrets.env", user.id, test_db, mock_settings
            )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "File not found for this run"


@pytest.mark.asyncio
async def test_get_result_file_requires_an_owned_run(test_db, mock_settings):
    user = AppUser(
        auth0_user_id="auth0|file-not-owner",
        name="File Not Owner",
        email="file-not-owner@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_file(
            "00000000-0000-0000-0000-000000000000", "any/key", user.id, test_db, mock_settings
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_get_result_file_maps_s3_service_error_to_502(
    test_db, persistent_models, mock_settings
):
    user, run, structure_key, _ = _make_boltz_prediction_run(test_db, "s3-error")

    mock_read = AsyncMock(side_effect=S3ServiceError("s3 upstream error"))
    with patch(
        "app.routes.workflow.results.read_result_output_file",
        new=mock_read,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_file(str(run.id), structure_key, user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "s3 upstream error"
    mock_read.assert_awaited_once_with(test_db, run, structure_key, settings=mock_settings)


@pytest.mark.asyncio
async def test_get_result_file_maps_s3_configuration_error_to_500(
    test_db, persistent_models, mock_settings
):
    user, run, structure_key, _ = _make_boltz_prediction_run(test_db, "s3-config")

    with patch(
        "app.routes.workflow.results.read_result_output_file",
        new=AsyncMock(side_effect=S3ConfigurationError("s3 config missing")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_result_file(str(run.id), structure_key, user.id, test_db, mock_settings)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "s3 config missing"
