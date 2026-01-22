"""Tests for dataset service."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services.datasets import (
    DatasetCreationResult,
    DatasetUploadResult,
    SeqeraConfigurationError,
    SeqeraServiceError,
    _get_required_env,
    _stringify_field,
    convert_form_data_to_csv,
    create_seqera_dataset,
    upload_dataset_to_seqera,
)


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


def test_get_required_env_success():
    """Test _get_required_env returns value when env var exists."""
    import os

    os.environ["TEST_ENV_VAR"] = "test_value"
    result = _get_required_env("TEST_ENV_VAR")
    assert result == "test_value"
    del os.environ["TEST_ENV_VAR"]


def test_get_required_env_missing():
    """Test _get_required_env raises error when env var is missing."""
    with pytest.raises(SeqeraConfigurationError, match="Missing required environment variable"):
        _get_required_env("NONEXISTENT_ENV_VAR")


def test_convert_simple_data():
    """Test converting simple form data to CSV."""
    form_data = {
        "name": "test",
        "value": "123",
        "flag": "true",
    }

    csv_output = convert_form_data_to_csv(form_data)

    lines = csv_output.strip().split("\n")
    assert len(lines) == 2  # header + 1 data row
    assert "name" in lines[0]
    assert "value" in lines[0]
    assert "flag" in lines[0]
    assert "test" in lines[1]
    assert "123" in lines[1]


def test_convert_with_numbers():
    """Test converting data with numeric values."""
    form_data = {
        "sample_id": "sample_001",
        "count": 42,
        "ratio": 3.14,
    }

    csv_output = convert_form_data_to_csv(form_data)

    assert "42" in csv_output
    assert "3.14" in csv_output


def test_convert_with_list():
    """Test converting data with list values."""
    form_data = {
        "sample": "test",
        "files": ["file1.txt", "file2.txt"],
    }

    csv_output = convert_form_data_to_csv(form_data)

    assert "file1.txt;file2.txt" in csv_output


def test_convert_with_dict():
    """Test converting data with dict values."""
    form_data = {
        "sample": "test",
        "metadata": {"type": "experiment", "id": 1},
    }

    csv_output = convert_form_data_to_csv(form_data)

    assert "metadata" in csv_output
    assert "type" in csv_output or "experiment" in csv_output


def test_convert_empty_data_raises_error():
    """Test that empty form data raises ValueError."""
    with pytest.raises(ValueError, match="formData cannot be empty"):
        convert_form_data_to_csv({})


def test_convert_with_none_values():
    """Test converting data with None values."""
    form_data = {
        "sample": "test",
        "optional_field": None,
    }

    csv_output = convert_form_data_to_csv(form_data)

    lines = csv_output.strip().split("\n")
    assert len(lines) == 2


@pytest.mark.asyncio
@respx.mock
async def test_create_dataset_success():
    """Test successful dataset creation."""
    route = respx.post(url__regex=r".*/workspaces/.*/datasets/").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset": {
                    "id": "dataset_123",
                    "name": "test-dataset",
                }
            },
        )
    )

    result = await create_seqera_dataset(name="test-dataset", description="Test description")

    assert isinstance(result, DatasetCreationResult)
    assert result.dataset_id == "dataset_123"
    assert result.raw_response["dataset"]["name"] == "test-dataset"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_create_dataset_default_name():
    """Test dataset creation with auto-generated name."""
    route = respx.post(url__regex=r".*/workspaces/.*/datasets/").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset": {
                    "id": "dataset_456",
                    "name": "dataset-1234567890",
                }
            },
        )
    )

    result = await create_seqera_dataset()

    assert result.dataset_id == "dataset_456"
    # Verify a name was generated
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_create_dataset_api_error():
    """Test dataset creation with API error."""
    respx.post(url__regex=r".*/workspaces/.*/datasets/").mock(
        return_value=httpx.Response(400, text="Bad request")
    )

    with pytest.raises(SeqeraServiceError, match="400"):
        await create_seqera_dataset(name="test")


@pytest.mark.asyncio
@respx.mock
async def test_create_dataset_missing_id_in_response():
    """Test handling when response is missing dataset ID."""
    respx.post(url__regex=r".*/workspaces/.*/datasets/").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset": {
                    "name": "test-dataset",
                    # Missing "id" field
                }
            },
        )
    )

    with pytest.raises(SeqeraServiceError, match="response lacked dataset id"):
        await create_seqera_dataset(name="test")


@pytest.mark.asyncio
@respx.mock
async def test_upload_success():
    """Test successful dataset upload."""
    route = respx.post(url__regex=r".*/workspaces/.*/datasets/.*/upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": {"datasetId": "dataset_789"},
                "message": "Upload successful",
            },
        )
    )

    form_data = {
        "sample": "test_sample",
        "input": "/path/file.txt",
    }

    result = await upload_dataset_to_seqera(dataset_id="dataset_789", form_data=form_data)

    assert isinstance(result, DatasetUploadResult)
    assert result.success is True
    assert result.dataset_id == "dataset_789"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_upload_creates_csv():
    """Test that upload creates proper CSV."""
    route = respx.post(url__regex=r".*/workspaces/.*/datasets/.*/upload").mock(
        return_value=httpx.Response(200, json={})
    )

    form_data = {
        "col1": "value1",
        "col2": "value2",
    }

    await upload_dataset_to_seqera("dataset_123", form_data)

    # Verify POST was called
    assert route.called
    # The CSV data should be in the request


@pytest.mark.asyncio
async def test_upload_empty_dataset_id():
    """Test upload fails with empty dataset_id."""
    with pytest.raises(ValueError, match="dataset_id is required"):
        await upload_dataset_to_seqera("", {"sample": "test"})


@pytest.mark.asyncio
async def test_upload_empty_form_data():
    """Test upload fails with empty form_data."""
    with pytest.raises(ValueError, match="formData cannot be empty"):
        await upload_dataset_to_seqera("dataset_123", {})


@pytest.mark.asyncio
@respx.mock
async def test_upload_api_error():
    """Test upload with API error."""
    respx.post(url__regex=r".*/workspaces/.*/datasets/.*/upload").mock(
        return_value=httpx.Response(500, text="Server error")
    )

    form_data = {"sample": "test"}

    with pytest.raises(SeqeraServiceError, match="500"):
        await upload_dataset_to_seqera("dataset_123", form_data)


@pytest.mark.asyncio
@respx.mock
async def test_upload_with_complex_data():
    """Test upload with complex form data."""
    respx.post(url__regex=r".*/workspaces/.*/datasets/.*/upload").mock(
        return_value=httpx.Response(200, json={})
    )

    form_data = {
        "sample": "test",
        "files": ["file1.txt", "file2.txt"],
        "count": 42,
        "metadata": {"type": "test"},
    }

    result = await upload_dataset_to_seqera("dataset_123", form_data)

    assert result.success is True
