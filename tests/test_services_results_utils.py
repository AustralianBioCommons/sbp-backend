"""Tests for result-specific formatting helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.db.models.core import AppUser, RunOutput, S3Object, WorkflowRun
from app.services.results_utils import (
    ClassifiedOutput,
    WORKFLOW_OUTPUT_SPECS,
    WorkflowResultsSpec,
    _build_s3_uri,
    build_alphafold2_proteinfold_output_listing_prefixes,
    build_bindcraft_output_listing_prefixes,
    build_boltz_proteinfold_output_listing_prefixes,
    build_colabfold_proteinfold_output_listing_prefixes,
    classify_alphafold2_proteinfold_output,
    classify_bindcraft_output_key,
    classify_boltz_proteinfold_output,
    classify_colabfold_proteinfold_output,
    extract_bindcraft_max_score,
    extract_proteinfold_max_score,
    format_log_entries,
    get_bindcraft_score_file,
    get_proteinfold_score_file,
    get_sample_id_for_result,
    get_tool_name,
    resolve_pdb_presigned_urls,
    resolve_submitted_form_data,
    s3_uri_to_key,
)
from app.services.s3 import S3ServiceError


def test_format_log_entries_extracts_timestamp_level_and_strips_ansi():
    result = format_log_entries(
        [
            "2026-03-10T10:00:00Z INFO Starting workflow",
            "plain line without metadata",
            "2026-03-10 10:01:00 WARNING Queue is full",
            "  \u001b[0;34mworkDir                   : \u001b[0;32m/scratch/yz52/sbp/workdir\u001b[0m",
        ]
    )

    assert len(result) == 4
    assert result[0].timestamp == "2026-03-10T10:00:00Z"
    assert result[0].level == "INFO"
    assert result[0].message == "INFO Starting workflow"
    assert result[1].timestamp is None
    assert result[1].level == "INFO"
    assert result[1].message == "plain line without metadata"
    assert result[2].level == "WARN"
    assert "\u001b[" not in result[3].message
    assert result[3].message == "workDir                   : /scratch/yz52/sbp/workdir"


def test_resolve_submitted_form_data_prefers_stored_payload():
    run = SimpleNamespace(submitted_form_data={"id": "stored", "binder_name": "binder"})

    assert resolve_submitted_form_data(run) == {"id": "stored", "binder_name": "binder"}


def test_resolve_submitted_form_data_builds_fallback_payload():
    run = SimpleNamespace(
        submitted_form_data=None,
        sample_id="sample-1",
        binder_name="binder-a",
        metrics=SimpleNamespace(final_design_count=7),
    )

    assert resolve_submitted_form_data(run) == {
        "id": "sample-1",
        "binder_name": "binder-a",
        "number_of_final_designs": 7,
        "_source": "fallback_local",
        "_warning": "submitted_form_data_missing",
    }


def test_resolve_submitted_form_data_returns_none_when_nothing_available():
    run = SimpleNamespace(submitted_form_data=None, sample_id=None, binder_name=None, metrics=None)

    assert resolve_submitted_form_data(run) is None


def test_get_tool_name_uses_tool_column():
    run = SimpleNamespace(tool="colabfold", submitted_form_data=None)
    assert get_tool_name(run) == "colabfold"


def test_get_tool_name_strips_and_lowercases_tool_column():
    run = SimpleNamespace(tool="  BindCraft  ", submitted_form_data=None)
    assert get_tool_name(run) == "bindcraft"


def test_get_tool_name_falls_back_to_form_data_tool_key():
    run = SimpleNamespace(tool=None, submitted_form_data={"tool": "alphafold2"})
    assert get_tool_name(run) == "alphafold2"


def test_get_tool_name_strips_and_lowercases_form_data_tool_key():
    run = SimpleNamespace(tool=None, submitted_form_data={"tool": "  AlphaFold2  "})
    assert get_tool_name(run) == "alphafold2"


def test_get_tool_name_falls_back_to_form_data_mode_key():
    run = SimpleNamespace(tool=None, submitted_form_data={"mode": "boltz"})
    assert get_tool_name(run) == "boltz"


def test_get_tool_name_tool_column_takes_priority():
    run = SimpleNamespace(tool="colabfold", submitted_form_data={"mode": "alphafold2"})
    assert get_tool_name(run) == "colabfold"


def test_get_tool_name_returns_none_when_nothing_available():
    run = SimpleNamespace(tool=None, submitted_form_data=None)
    assert get_tool_name(run) is None


def test_s3_uri_to_key_handles_empty_non_s3_and_invalid_s3_values():
    assert s3_uri_to_key(None) is None
    assert s3_uri_to_key("   ") is None
    assert s3_uri_to_key("plain/key.txt") == "plain/key.txt"
    assert s3_uri_to_key("s3://bucket") is None
    assert s3_uri_to_key("s3://bucket/path/to/file.txt") == "path/to/file.txt"


def test_get_sample_id_for_result_uses_fallback_order_and_strips():
    run_with_sample_id = SimpleNamespace(
        submitted_form_data={"sample_id": " form-sample-1 ", "samplesheetId": " sheet-1 "},
        sample_id="sample-1",
        binder_name="binder-1",
        form_id="form-1",
    )
    run_with_form_sample_id = SimpleNamespace(
        submitted_form_data={"sample_id": " form-sample-1 ", "id": "id-1"},
        sample_id=None,
        binder_name="binder-1",
        form_id="form-1",
    )
    run_with_form_id = SimpleNamespace(
        submitted_form_data={"id": " id-1 ", "samplesheetId": "sheet-1"},
        sample_id=None,
        binder_name="binder-1",
        form_id="form-1",
    )
    run_with_samplesheet_id = SimpleNamespace(
        submitted_form_data={"samplesheetId": " sheet-1 "},
        sample_id=None,
        binder_name="binder-1",
        form_id="form-1",
    )
    run_with_binder = SimpleNamespace(sample_id=None, binder_name=" binder-1 ", form_id="form-1")
    run_with_form = SimpleNamespace(sample_id=None, binder_name=None, form_id=" form-2 ")
    run_empty = SimpleNamespace(sample_id=None, binder_name=None, form_id=None)

    assert get_sample_id_for_result(run_with_sample_id) == "sample-1"
    assert get_sample_id_for_result(run_with_form_sample_id) == "form-sample-1"
    assert get_sample_id_for_result(run_with_form_id) == "id-1"
    assert get_sample_id_for_result(run_with_samplesheet_id) == "sheet-1"
    assert get_sample_id_for_result(run_with_binder) == "binder-1"
    assert get_sample_id_for_result(run_with_form) == "form-2"
    assert get_sample_id_for_result(run_empty) is None


def test_bindcraft_helpers_classify_keys_and_build_prefixes(monkeypatch):
    run = WorkflowRun(id=uuid4(), owner_user_id=uuid4(), sample_id="sampleZ")

    assert classify_bindcraft_output_key(" ") is None
    assert classify_bindcraft_output_key("folder/") is None
    assert classify_bindcraft_output_key(f"{run.id}/Accepted/Animation/report.html") is None
    assert classify_bindcraft_output_key(
        f"{run.id}/generate/bindcraft_report.html"
    ) == ClassifiedOutput(
        "report",
        "bindcraft_report.html",
    )
    assert classify_bindcraft_output_key(
        f"{run.id}/bindcraft/sampleZ_0_output/preview.png"
    ) == ClassifiedOutput(
        "snapshot",
        "preview.png",
    )
    assert classify_bindcraft_output_key(
        f"{run.id}/ranker/sampleZ_ranked/model.pdb"
    ) == ClassifiedOutput(
        "pdb",
        "model.pdb",
    )
    assert classify_bindcraft_output_key(
        f"{run.id}/ranker/sampleZ_final_design_stats.csv"
    ) == ClassifiedOutput(
        "stats_csv",
        "sampleZ_final_design_stats.csv",
    )

    prefixes = build_bindcraft_output_listing_prefixes(run)
    assert prefixes == [
        f"{run.id}/ranker/",
        f"{run.id}/generate/",
        f"{run.id}/bindcraft/sampleZ_0_output/",
    ]

    run_without_sample = SimpleNamespace(id=run.id, sample_id=None, binder_name=None, form_id=None)
    assert build_bindcraft_output_listing_prefixes(run_without_sample) == [
        f"{run.id}/ranker/",
        f"{run.id}/generate/",
    ]

    monkeypatch.setenv("AWS_S3_BUCKET", "test-bucket")
    assert _build_s3_uri("path/to/file.txt") == "s3://test-bucket/path/to/file.txt"
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    assert _build_s3_uri("path/to/file.txt") == "path/to/file.txt"


def test_get_bindcraft_score_file_uses_final_design_stats():
    keys = [
        "run/ranker/model.pdb",
        "run/ranker/s1_final_design_stats.csv",
        "run/generate/report.html",
    ]

    assert get_bindcraft_score_file(keys, "s1") == "run/ranker/s1_final_design_stats.csv"


def test_get_bindcraft_score_file_returns_none_without_stats():
    keys = [
        "run/ranker/model.pdb",
        "run/generate/report.html",
    ]

    assert get_bindcraft_score_file(keys, "s1") is None


@pytest.mark.asyncio
async def test_extract_bindcraft_max_score_reads_average_i_ptm():
    csv_text = "design_id,Average_i_pTM\nA,0.12\nB,0.91\nC,\n"

    with patch(
        "app.services.results_utils.read_s3_file",
        new_callable=AsyncMock,
        return_value=csv_text,
    ) as read_file:
        score = await extract_bindcraft_max_score("run/ranker/s1_final_design_stats.csv")

    assert score == 0.91
    read_file.assert_awaited_once_with("run/ranker/s1_final_design_stats.csv")


@pytest.mark.parametrize(
    ("tool", "key"),
    [
        ("boltz", "run-1/boltz/T1024/T1024_0_ptm.tsv"),
        ("alphafold2", "run-1/alphafold2/split_msa_prediction/T1024/T1024_0_ptm.tsv"),
        ("colabfold", "run-1/colabfold/T1024/T1024_0_ptm.tsv"),
    ],
)
def test_get_proteinfold_score_file_matches_single_prediction_tool_paths(tool, key):
    keys = [
        f"run-1/{tool}/T1024/T1024_0_pae.tsv",
        key,
        f"run-1/{tool}/T1024/other.tsv",
    ]

    assert get_proteinfold_score_file(keys, "T1024") == key


@pytest.mark.asyncio
async def test_extract_proteinfold_max_score_reads_ranked_tsv():
    tsv_text = "1\t0.42\n0\t0.91\n2\t0.11\n"

    with patch(
        "app.services.results_utils.read_s3_file",
        new_callable=AsyncMock,
        return_value=tsv_text,
    ) as read_file:
        score = await extract_proteinfold_max_score("run-1/boltz/T1024/T1024_0_ptm.tsv")

    assert score == 0.91
    read_file.assert_awaited_once_with("run-1/boltz/T1024/T1024_0_ptm.tsv")


def test_all_workflow_output_specs_have_score_hooks():
    for workflow_specs in WORKFLOW_OUTPUT_SPECS.values():
        for spec in workflow_specs.values():
            assert spec.get_score_file is not None
            assert spec.extract_max_score is not None


@pytest.mark.asyncio
async def test_workflow_results_spec_get_max_score_returns_none_without_score_file(test_db):
    user = AppUser(
        auth0_user_id="auth0|score-none-user",
        name="Score None User",
        email="score-none@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="score-none-run",
        sample_id="T1024",
        work_dir="workdir-score-none",
    )
    test_db.add_all([user, run])
    test_db.commit()

    extractor = AsyncMock(return_value=0.91)
    spec = WorkflowResultsSpec(
        kind="single-prediction",
        tool="boltz",
        required_categories=set(),
        get_prefixes=lambda _run: [],
        classify=lambda _key, _sample_id: None,
        get_score_file=lambda _keys, _sample_id: None,
        extract_max_score=extractor,
    )

    assert await spec.get_max_score(test_db, run) is None
    extractor.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_results_spec_get_max_score_extracts_selected_run_output(test_db):
    user = AppUser(
        auth0_user_id="auth0|score-selected-user",
        name="Score Selected User",
        email="score-selected@example.com",
    )
    run = WorkflowRun(
        owner=user,
        seqera_run_id="score-selected-run",
        sample_id="T1024",
        work_dir="workdir-score-selected",
    )
    ignored = S3Object(
        object_key="run-1/boltz/T1024/T1024_0_pae.tsv",
        uri="s3://bucket/run-1/boltz/T1024/T1024_0_pae.tsv",
    )
    score_object = S3Object(
        object_key="run-1/boltz/T1024/T1024_0_ptm.tsv",
        uri="s3://bucket/run-1/boltz/T1024/T1024_0_ptm.tsv",
    )
    test_db.add_all([user, run, ignored, score_object])
    test_db.flush()
    test_db.add_all(
        [
            RunOutput(run_id=run.id, s3_object_id=ignored.object_key),
            RunOutput(run_id=run.id, s3_object_id=score_object.object_key),
        ]
    )
    test_db.commit()

    extractor = AsyncMock(return_value=0.91)
    spec = WorkflowResultsSpec(
        kind="single-prediction",
        tool="boltz",
        required_categories=set(),
        get_prefixes=lambda _run: [],
        classify=lambda _key, _sample_id: None,
        get_score_file=get_proteinfold_score_file,
        extract_max_score=extractor,
    )

    assert await spec.get_max_score(test_db, run) == 0.91
    extractor.assert_awaited_once_with("run-1/boltz/T1024/T1024_0_ptm.tsv")


def test_boltz_proteinfold_helpers_classify_keys_and_build_prefixes():
    run = WorkflowRun(id=uuid4(), owner_user_id=uuid4(), sample_id="T1024")

    assert classify_boltz_proteinfold_output(" ") is None
    assert classify_boltz_proteinfold_output("folder/") is None

    # With sample_id, paths use the sample_id name
    assert classify_boltz_proteinfold_output(
        f"{run.id}/reports/T1024_boltz_report.html", "T1024"
    ) == ClassifiedOutput("report", "T1024_boltz_report.html")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/boltz/top_ranked_structures/T1024.pdb", "T1024"
    ) == ClassifiedOutput("pdb", "T1024.pdb")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/boltz/T1024/abcd1234.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/boltz/T1024/paes/abcd1234.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/mmseqs/T1024.a3m", "T1024"
    ) == ClassifiedOutput("alignment", "T1024.a3m")

    # "single_prediction" paths do not match when sample_id is set
    assert (
        classify_boltz_proteinfold_output(
            f"{run.id}/boltz/top_ranked_structures/single_prediction.pdb", "T1024"
        )
        is None
    )
    assert (
        classify_boltz_proteinfold_output(f"{run.id}/boltz/single_prediction/abcd1234.tsv", "T1024")
        is None
    )
    assert (
        classify_boltz_proteinfold_output(f"{run.id}/mmseqs/single_prediction.a3m", "T1024") is None
    )

    # Without sample_id, falls back to matching "single_prediction"
    assert classify_boltz_proteinfold_output(
        f"{run.id}/boltz/top_ranked_structures/single_prediction.pdb"
    ) == ClassifiedOutput("pdb", "single_prediction.pdb")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/boltz/single_prediction/abcd1234.tsv"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_boltz_proteinfold_output(
        f"{run.id}/mmseqs/single_prediction.a3m"
    ) == ClassifiedOutput("alignment", "single_prediction.a3m")

    assert build_boltz_proteinfold_output_listing_prefixes(run) == [
        f"{run.id}/reports/",
        f"{run.id}/boltz/top_ranked_structures/",
        f"{run.id}/mmseqs/",
        f"{run.id}/boltz/T1024/",
    ]


def test_alphafold2_proteinfold_helpers_classify_keys_and_build_prefixes():
    run = WorkflowRun(id=uuid4(), owner_user_id=uuid4(), sample_id="T1024")

    # With sample_id, paths use the sample_id name
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/reports/T1024_alphafold2_report.html", "T1024"
    ) == ClassifiedOutput("report", "T1024_alphafold2_report.html")
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/top_ranked_structures/T1024.pdb", "T1024"
    ) == ClassifiedOutput("pdb", "T1024.pdb")
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/T1024/abcd1234.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/T1024/paes/T1024_0_pae.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "T1024_0_pae.tsv")
    # No alignment expected for alphafold2
    assert (
        classify_alphafold2_proteinfold_output(f"{run.id}/mmseqs/results/T1024.a3m", "T1024")
        is None
    )

    # "single_prediction" paths do not match when sample_id is set
    assert (
        classify_alphafold2_proteinfold_output(
            f"{run.id}/alphafold2/split_msa_prediction/top_ranked_structures/single_prediction.pdb",
            "T1024",
        )
        is None
    )
    assert (
        classify_alphafold2_proteinfold_output(
            f"{run.id}/alphafold2/split_msa_prediction/single_prediction/abcd1234.tsv", "T1024"
        )
        is None
    )

    # Without sample_id, falls back to matching "single_prediction"
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/top_ranked_structures/single_prediction.pdb"
    ) == ClassifiedOutput("pdb", "single_prediction.pdb")
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/single_prediction/abcd1234.tsv"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_alphafold2_proteinfold_output(
        f"{run.id}/alphafold2/split_msa_prediction/single_prediction/paes/T1024_0_pae.tsv"
    ) == ClassifiedOutput("stats_csv", "T1024_0_pae.tsv")

    assert build_alphafold2_proteinfold_output_listing_prefixes(run) == [
        f"{run.id}/reports/",
        f"{run.id}/alphafold2/split_msa_prediction/top_ranked_structures/",
        f"{run.id}/alphafold2/split_msa_prediction/T1024/",
    ]


def test_colabfold_proteinfold_helpers_classify_keys_and_build_prefixes():
    run = WorkflowRun(id=uuid4(), owner_user_id=uuid4(), sample_id="T1024")

    # With sample_id, paths use the sample_id name
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/reports/T1024_colabfold_report.html", "T1024"
    ) == ClassifiedOutput("report", "T1024_colabfold_report.html")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/top_ranked_structures/T1024.pdb", "T1024"
    ) == ClassifiedOutput("pdb", "T1024.pdb")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/T1024/abcd1234.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/T1024/paes/T1024_0_pae.tsv", "T1024"
    ) == ClassifiedOutput("stats_csv", "T1024_0_pae.tsv")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/mmseqs/T1024.a3m", "T1024"
    ) == ClassifiedOutput("alignment", "T1024.a3m")

    # "single_prediction" paths do not match when sample_id is set
    assert (
        classify_colabfold_proteinfold_output(
            f"{run.id}/colabfold/top_ranked_structures/single_prediction.pdb", "T1024"
        )
        is None
    )
    assert (
        classify_colabfold_proteinfold_output(
            f"{run.id}/colabfold/single_prediction/abcd1234.tsv", "T1024"
        )
        is None
    )
    assert (
        classify_colabfold_proteinfold_output(f"{run.id}/mmseqs/single_prediction.a3m", "T1024")
        is None
    )

    # Without sample_id, falls back to matching "single_prediction"
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/top_ranked_structures/single_prediction.pdb"
    ) == ClassifiedOutput("pdb", "single_prediction.pdb")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/single_prediction/abcd1234.tsv"
    ) == ClassifiedOutput("stats_csv", "abcd1234.tsv")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/colabfold/single_prediction/paes/T1024_0_pae.tsv"
    ) == ClassifiedOutput("stats_csv", "T1024_0_pae.tsv")
    assert classify_colabfold_proteinfold_output(
        f"{run.id}/mmseqs/single_prediction.a3m"
    ) == ClassifiedOutput("alignment", "single_prediction.a3m")

    assert build_colabfold_proteinfold_output_listing_prefixes(run) == [
        f"{run.id}/reports/",
        f"{run.id}/colabfold/top_ranked_structures/",
        f"{run.id}/mmseqs/",
        f"{run.id}/colabfold/T1024/",
    ]


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_replaces_starting_pdb_s3_uri():
    presigned = "https://my-bucket.s3.amazonaws.com/uploads/target.pdb?X-Amz-Signature=test"
    form_data = {"binder_name": "PDL1", "starting_pdb": "s3://my-bucket/uploads/target.pdb"}

    with patch(
        "app.services.results_utils.generate_presigned_url",
        new=AsyncMock(return_value=presigned),
    ):
        result = await resolve_pdb_presigned_urls(form_data)

    assert result["binder_name"] == "PDL1"
    assert result["starting_pdb"] == presigned


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_sanitizes_content_disposition_filename():
    presigned = "https://my-bucket.s3.amazonaws.com/uploads/target.pdb?X-Amz-Signature=test"
    form_data = {
        "starting_pdb": 's3://my-bucket/uploads/bad"name\r\nX-Injected: yes.pdb',
    }

    with patch(
        "app.services.results_utils.generate_presigned_url",
        new=AsyncMock(return_value=presigned),
    ) as mocked_presign:
        result = await resolve_pdb_presigned_urls(form_data)

    assert result["starting_pdb"] == presigned
    mocked_presign.assert_awaited_once()
    content_disposition = mocked_presign.await_args.kwargs["response_content_disposition"]
    assert content_disposition.startswith('attachment; filename="')
    assert "\r" not in content_disposition
    assert "\n" not in content_disposition
    assert '"name' not in content_disposition
    assert "X-Injected: yes" not in content_disposition


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_logs_presign_failures(caplog):
    form_data = {"starting_pdb": "s3://my-bucket/uploads/target.pdb"}

    with (
        caplog.at_level("WARNING", logger="app.services.results_utils"),
        patch(
            "app.services.results_utils.generate_presigned_url",
            new=AsyncMock(side_effect=S3ServiceError("presign failed")),
        ),
    ):
        result = await resolve_pdb_presigned_urls(form_data)

    assert result == form_data
    assert "Failed to generate presigned starting_pdb URL" in caplog.text
    assert "uploads/target.pdb" in caplog.text


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_passthrough_when_no_starting_pdb():
    form_data = {"binder_name": "PDL1", "min_length": 60}
    result = await resolve_pdb_presigned_urls(form_data)
    assert result == form_data


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_passthrough_when_not_s3_uri():
    form_data = {"starting_pdb": "https://cdn.example.com/target.pdb"}
    result = await resolve_pdb_presigned_urls(form_data)
    assert result == form_data


@pytest.mark.asyncio
async def test_resolve_pdb_presigned_urls_returns_none_for_none_input():
    result = await resolve_pdb_presigned_urls(None)
    assert result is None
