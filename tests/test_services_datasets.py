"""Tests for dataset service."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

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


class TestStringifyField:
    """Tests for _stringify_field helper."""

    def test_stringify_none(self):
        """Test stringifying None returns empty string."""
        assert _stringify_field(None) == ""

    def test_stringify_string(self):
        """Test stringifying a string."""
        assert _stringify_field("hello") == "hello"

    def test_stringify_number(self):
        """Test stringifying a number."""
        assert _stringify_field(42) == "42"
        assert _stringify_field(3.14) == "3.14"

    def test_stringify_list(self):
        """Test stringifying a list."""
        assert _stringify_field(["a", "b", "c"]) == "a;b;c"

    def test_stringify_list_with_none(self):
        """Test stringifying a list containing None."""
        assert _stringify_field(["a", None, "c"]) == "a;;c"

    def test_stringify_dict(self):
        """Test stringifying a dict as JSON."""
        result = _stringify_field({"key": "value", "num": 42})
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["num"] == 42

    def test_stringify_boolean(self):
        """Test stringifying boolean."""
        assert _stringify_field(True) == "True"
        assert _stringify_field(False) == "False"


class TestConvertFormDataToCsv:
    """Tests for convert_form_data_to_csv function."""

    def test_convert_simple_data(self):
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

    def test_convert_with_numbers(self):
        """Test converting data with numeric values."""
        form_data = {
            "sample_id": "sample_001",
            "count": 42,
            "ratio": 3.14,
        }
        
        csv_output = convert_form_data_to_csv(form_data)
        
        assert "42" in csv_output
        assert "3.14" in csv_output

    def test_convert_with_list(self):
        """Test converting data with list values."""
        form_data = {
            "sample": "test",
            "files": ["file1.txt", "file2.txt"],
        }
        
        csv_output = convert_form_data_to_csv(form_data)
        
        assert "file1.txt;file2.txt" in csv_output

    def test_convert_with_dict(self):
        """Test converting data with dict values."""
        form_data = {
            "sample": "test",
            "metadata": {"type": "experiment", "id": 1},
        }
        
        csv_output = convert_form_data_to_csv(form_data)
        
        assert "metadata" in csv_output
        assert "type" in csv_output or "experiment" in csv_output

    def test_convert_empty_data_raises_error(self):
        """Test that empty form data raises ValueError."""
        with pytest.raises(ValueError, match="formData cannot be empty"):
            convert_form_data_to_csv({})

    def test_convert_with_none_values(self):
        """Test converting data with None values."""
        form_data = {
            "sample": "test",
            "optional_field": None,
        }
        
        csv_output = convert_form_data_to_csv(form_data)
        
        lines = csv_output.strip().split("\n")
        assert len(lines) == 2


class TestCreateSeqeraDataset:
    """Tests for create_seqera_dataset function."""

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_create_dataset_success(self, mock_client_class):
        """Test successful dataset creation."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.json.return_value = {
            "id": "dataset_123",
            "name": "test-dataset",
        }
        mock_response.status_code = 200
        mock_response.text = ""
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        result = await create_seqera_dataset(
            name="test-dataset",
            description="Test description"
        )
        
        assert isinstance(result, DatasetCreationResult)
        assert result.dataset_id == "dataset_123"
        assert result.raw_response["name"] == "test-dataset"

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_create_dataset_default_name(self, mock_client_class):
        """Test dataset creation with auto-generated name."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.json.return_value = {
            "id": "dataset_456",
            "name": "dataset-1234567890",
        }
        mock_response.status_code = 200
        mock_response.text = ""
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        result = await create_seqera_dataset()
        
        assert result.dataset_id == "dataset_456"
        # Verify a name was generated
        mock_client.post.assert_called_once()

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_create_dataset_api_error(self, mock_client_class):
        """Test dataset creation with API error."""
        mock_response = AsyncMock()
        mock_response.is_error = True
        mock_response.status_code = 400
        mock_response.text = "Bad request"
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        with pytest.raises(SeqeraServiceError, match="400"):
            await create_seqera_dataset(name="test")

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_create_dataset_missing_id_in_response(self, mock_client_class):
        """Test handling when response is missing dataset ID."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.json.return_value = {
            "name": "test-dataset",
            # Missing "id" field
        }
        mock_response.status_code = 200
        mock_response.text = "{}"
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        with pytest.raises(SeqeraServiceError, match="Failed to extract dataset ID"):
            await create_seqera_dataset(name="test")


class TestUploadDatasetToSeqera:
    """Tests for upload_dataset_to_seqera function."""

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_upload_success(self, mock_client_class):
        """Test successful dataset upload."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.status_code = 200
        mock_response.text = ""
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        form_data = {
            "sample": "test_sample",
            "input": "/path/file.txt",
        }
        
        result = await upload_dataset_to_seqera(
            dataset_id="dataset_789",
            form_data=form_data
        )
        
        assert isinstance(result, DatasetUploadResult)
        assert result.success is True
        assert result.dataset_id == "dataset_789"

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_upload_creates_csv(self, mock_client_class):
        """Test that upload creates proper CSV."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.status_code = 200
        mock_response.text = ""
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        form_data = {
            "col1": "value1",
            "col2": "value2",
        }
        
        await upload_dataset_to_seqera("dataset_123", form_data)
        
        # Verify POST was called with files
        call_args = mock_client.post.call_args
        assert "files" in call_args[1]

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_upload_api_error(self, mock_client_class):
        """Test upload with API error."""
        mock_response = AsyncMock()
        mock_response.is_error = True
        mock_response.status_code = 500
        mock_response.text = "Server error"
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        form_data = {"sample": "test"}
        
        with pytest.raises(SeqeraServiceError, match="500"):
            await upload_dataset_to_seqera("dataset_123", form_data)

    @patch("app.services.datasets.httpx.AsyncClient")
    async def test_upload_with_complex_data(self, mock_client_class):
        """Test upload with complex form data."""
        mock_response = AsyncMock()
        mock_response.is_error = False
        mock_response.status_code = 200
        mock_response.text = ""
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_client
        
        form_data = {
            "sample": "test",
            "files": ["file1.txt", "file2.txt"],
            "count": 42,
            "metadata": {"type": "test"},
        }
        
        result = await upload_dataset_to_seqera("dataset_123", form_data)
        
        assert result.success is True
