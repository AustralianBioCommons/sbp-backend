"""Tests for dataset service."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.schemas.workflows import WispsSequenceItem
from app.services.datasets import (
    BULK_PREDICTION_BASE_PATH,
    INTERACTION_SCREENING_BASE_PATH,
    _stringify_field,
    build_unique_dataset_name,
    convert_form_data_to_csv,
    upload_csv_to_s3,
    upload_wisps_samplesheet_to_s3,
)
from app.services.s3 import S3UploadResult


def _s3_result(key: str = "inputs/samplesheets/samplesheet.csv") -> S3UploadResult:
    return S3UploadResult(
        success=True, file_key=key, bucket="my-bucket", file_url=f"s3://my-bucket/{key}"
    )


# =============================================================================
# Tests for _stringify_field
# =============================================================================


def test_stringify_none():
    """Test stringifying None returns empty string."""
    assert _stringify_field(None) == ""


def test_stringify_string():
    """Test stringifying a string."""
    assert _stringify_field("hello") == "hello"


def test_stringify_number():
    """Test stringifying a number."""
    assert _stringify_field(42) == "42"
    assert _stringify_field(3.14) == "3.14"


def test_stringify_list():
    """Test stringifying a list."""
    assert _stringify_field(["a", "b", "c"]) == "a;b;c"


def test_stringify_list_with_none():
    """Test stringifying a list containing None."""
    assert _stringify_field(["a", None, "c"]) == "a;;c"


def test_stringify_dict():
    """Test stringifying a dict as JSON."""
    result = _stringify_field({"key": "value", "num": 42})
    parsed = json.loads(result)
    assert parsed["key"] == "value"
    assert parsed["num"] == 42


def test_stringify_boolean():
    """Test stringifying boolean."""
    assert _stringify_field(True) == "True"
    assert _stringify_field(False) == "False"


# =============================================================================
# Tests for build_unique_dataset_name
# =============================================================================


def test_build_unique_dataset_name_basic():
    """Unique name starts with the given slug."""
    name = build_unique_dataset_name("my-run")
    assert name.startswith("my-run_")
    assert len(name) > len("my-run_")


def test_build_unique_dataset_name_strips_special_chars():
    """Special characters are replaced with hyphens."""
    name = build_unique_dataset_name("run@name!")
    assert "@" not in name
    assert "!" not in name


def test_build_unique_dataset_name_empty_falls_back():
    """Empty input falls back to 'dataset' prefix."""
    name = build_unique_dataset_name("")
    assert name.startswith("dataset")


def test_build_unique_dataset_name_unique():
    """Two calls produce different names due to random suffix."""
    name_a = build_unique_dataset_name("run")
    name_b = build_unique_dataset_name("run")
    assert name_a != name_b


# =============================================================================
# Tests for convert_form_data_to_csv
# =============================================================================


def test_convert_simple_data():
    """Test converting simple form data to CSV."""
    form_data = {"name": "test", "value": "123", "flag": "true"}
    csv_output = convert_form_data_to_csv(form_data)
    lines = csv_output.strip().split("\n")
    assert len(lines) == 2
    assert "name" in lines[0]
    assert "value" in lines[0]
    assert "flag" in lines[0]
    assert "test" in lines[1]
    assert "123" in lines[1]


def test_convert_with_numbers():
    """Test converting data with numeric values."""
    csv_output = convert_form_data_to_csv({"sample_id": "s1", "count": 42, "ratio": 3.14})
    assert "42" in csv_output
    assert "3.14" in csv_output


def test_convert_with_list():
    """Test converting data with list values."""
    csv_output = convert_form_data_to_csv({"sample": "test", "files": ["file1.txt", "file2.txt"]})
    assert "file1.txt;file2.txt" in csv_output


def test_convert_with_dict():
    """Test converting data with dict values."""
    csv_output = convert_form_data_to_csv(
        {"sample": "test", "metadata": {"type": "experiment", "id": 1}}
    )
    assert "metadata" in csv_output


def test_convert_empty_data_raises_error():
    """Test that empty form data raises ValueError."""
    with pytest.raises(ValueError, match="formData cannot be empty"):
        convert_form_data_to_csv({})


def test_convert_with_none_values():
    """Test converting data with None values."""
    csv_output = convert_form_data_to_csv({"sample": "test", "optional_field": None})
    lines = csv_output.strip().split("\n")
    assert len(lines) == 2


# =============================================================================
# Tests for upload_csv_to_s3
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_csv_to_s3_success(mock_upload):
    """Test successful CSV upload to S3."""
    mock_upload.return_value = _s3_result()

    result = await upload_csv_to_s3({"sample": "test", "value": "123"})

    assert result.success is True
    assert result.bucket == "my-bucket"
    mock_upload.assert_called_once()
    _, kwargs = mock_upload.call_args
    assert kwargs["filename"] == "samplesheet.csv"
    assert kwargs["content_type"] == "text/csv"


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_csv_to_s3_empty_raises(mock_upload):
    """Test that empty form_data raises ValueError before upload."""
    with pytest.raises(ValueError, match="form_data cannot be empty"):
        await upload_csv_to_s3({})

    mock_upload.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_csv_to_s3_with_list_field(mock_upload):
    """Test upload with a list field serialised as semicolon-delimited."""
    mock_upload.return_value = _s3_result()

    await upload_csv_to_s3({"files": ["a.txt", "b.txt"], "sample": "s1"})

    mock_upload.assert_called_once()
    file_content = mock_upload.call_args.kwargs["file_content"].read().decode()
    assert "a.txt;b.txt" in file_content


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_csv_to_s3_returns_s3_result(mock_upload):
    """upload_csv_to_s3 passes the S3UploadResult through unchanged."""
    expected = _s3_result(key="inputs/samplesheets/custom.csv")
    mock_upload.return_value = expected

    result = await upload_csv_to_s3({"x": "y"})

    assert result is expected


# =============================================================================
# Tests for upload_interaction_screening_csv_to_s3
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_interaction_screening_success(mock_upload):
    """Test successful interaction screening samplesheet upload."""
    mock_upload.return_value = _s3_result()

    sequences = [
        WispsSequenceItem(id="q1", group="query"),
        WispsSequenceItem(id="t1", group="target"),
    ]
    result, split_output_dir = await upload_wisps_samplesheet_to_s3(
        sequences, "run-abc", INTERACTION_SCREENING_BASE_PATH, "interaction-screening", include_group=True
    )

    assert result.success is True
    assert "run-abc" in split_output_dir or "interaction_screening" in split_output_dir
    mock_upload.assert_called_once()


@pytest.mark.asyncio
async def test_upload_interaction_screening_empty_sequences_raises():
    """Empty sequences list raises ValueError."""
    with pytest.raises(ValueError, match="sequences cannot be empty"):
        await upload_wisps_samplesheet_to_s3(
            [], "run-1", INTERACTION_SCREENING_BASE_PATH, "interaction-screening", include_group=True
        )


@pytest.mark.asyncio
async def test_upload_interaction_screening_empty_run_id_raises():
    """Empty run_id raises ValueError."""
    with pytest.raises(ValueError, match="run_id is required"):
        await upload_wisps_samplesheet_to_s3(
            [WispsSequenceItem(id="s1", group="query")],
            "",
            INTERACTION_SCREENING_BASE_PATH,
            "interaction-screening",
            include_group=True,
        )


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_interaction_screening_csv_format(mock_upload):
    """query → g1, target → g2 in the generated CSV."""
    mock_upload.return_value = _s3_result()

    sequences = [
        WispsSequenceItem(id="q1", group="query"),
        WispsSequenceItem(id="t1", group="target"),
    ]
    await upload_wisps_samplesheet_to_s3(
        sequences, "my-run", INTERACTION_SCREENING_BASE_PATH, "interaction-screening", include_group=True
    )

    file_bytes = mock_upload.call_args.kwargs["file_content"].read().decode()
    assert "g1" in file_bytes
    assert "g2" in file_bytes
    assert "q1" in file_bytes
    assert "t1" in file_bytes
    assert "protein" in file_bytes


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_interaction_screening_split_output_dir_matches_run_path(mock_upload):
    """split_output_dir is derived from the same unique slug as the FASTA paths."""
    mock_upload.return_value = _s3_result()

    sequences = [WispsSequenceItem(id="s1", group="query")]
    _, split_output_dir = await upload_wisps_samplesheet_to_s3(
        sequences, "test-run", INTERACTION_SCREENING_BASE_PATH, "interaction-screening", include_group=True
    )

    file_bytes = mock_upload.call_args.kwargs["file_content"].read().decode()
    unique_slug = split_output_dir.split("/")[-1]
    assert unique_slug in file_bytes


@pytest.mark.asyncio
@patch("app.services.datasets.upload_file_to_s3")
async def test_upload_bulk_prediction_csv_format(mock_upload):
    """Bulk-prediction CSV has no group column."""
    mock_upload.return_value = _s3_result()

    sequences = [WispsSequenceItem(id="s1", sequence="MAGT"), WispsSequenceItem(id="s2", sequence="ACDE")]
    _, _ = await upload_wisps_samplesheet_to_s3(
        sequences, "bulk-run", BULK_PREDICTION_BASE_PATH, "bulk-prediction", include_group=False
    )

    file_bytes = mock_upload.call_args.kwargs["file_content"].read().decode()
    assert "group" not in file_bytes
    assert "s1" in file_bytes
    assert "s2" in file_bytes
    assert "protein" in file_bytes
