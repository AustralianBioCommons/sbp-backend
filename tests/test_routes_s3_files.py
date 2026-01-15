"""Tests for S3 file routes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.services.s3 import S3ConfigurationError, S3ServiceError


class TestListFilesEndpoint:
    """Tests for GET /api/s3/files endpoint."""

    def test_list_files_success(self, client: TestClient):
        """Test successful file listing."""
        mock_files = [
            {
                "key": "results/test/file1.csv",
                "size": 1024,
                "last_modified": "2026-01-15T10:00:00Z",
                "bucket": "test-bucket",
            },
            {
                "key": "results/test/file2.csv",
                "size": 2048,
                "last_modified": "2026-01-15T11:00:00Z",
                "bucket": "test-bucket",
            },
        ]

        with patch("app.routes.s3_files.list_s3_files", return_value=mock_files):
            response = client.get("/api/s3/files?prefix=results/test/")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total"] == 2
            assert len(data["files"]) == 2
            assert data["files"][0]["key"] == "results/test/file1.csv"

    def test_list_files_with_extension_filter(self, client: TestClient):
        """Test file listing with extension filter."""
        mock_files = [
            {
                "key": "results/test/file1.csv",
                "size": 1024,
                "last_modified": "2026-01-15T10:00:00Z",
                "bucket": "test-bucket",
            }
        ]

        with patch("app.routes.s3_files.list_s3_files", return_value=mock_files):
            response = client.get("/api/s3/files?prefix=results/test/&extension=.csv")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total"] == 1

    def test_list_files_empty_result(self, client: TestClient):
        """Test file listing with no results."""
        with patch("app.routes.s3_files.list_s3_files", return_value=[]):
            response = client.get("/api/s3/files?prefix=nonexistent/")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total"] == 0
            assert data["files"] == []

    def test_list_files_configuration_error(self, client: TestClient):
        """Test file listing with configuration error."""
        with patch(
            "app.routes.s3_files.list_s3_files",
            side_effect=S3ConfigurationError("AWS_S3_BUCKET not set"),
        ):
            response = client.get("/api/s3/files")

            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert "S3 configuration error" in response.json()["detail"]

    def test_list_files_service_error(self, client: TestClient):
        """Test file listing with service error."""
        with patch(
            "app.routes.s3_files.list_s3_files",
            side_effect=S3ServiceError("S3 connection failed"),
        ):
            response = client.get("/api/s3/files")

            assert response.status_code == status.HTTP_502_BAD_GATEWAY
            assert "S3 service error" in response.json()["detail"]


class TestReadCsvFileEndpoint:
    """Tests for GET /api/s3/csv/{file_key:path} endpoint."""

    def test_read_csv_all_columns(self, client: TestClient):
        """Test reading CSV with all columns."""
        mock_data = [
            {"Design": "design1", "Average_i_pTM": "0.84", "Rank": "1"},
            {"Design": "design2", "Average_i_pTM": "0.78", "Rank": "2"},
        ]

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_data):
            response = client.get("/api/s3/csv/results/test/file.csv")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total_rows"] == 2
            assert len(data["data"]) == 2
            assert data["columns"] == ["Design", "Average_i_pTM", "Rank"]

    def test_read_csv_selected_columns(self, client: TestClient):
        """Test reading CSV with selected columns."""
        mock_data = [
            {"Design": "design1", "Average_i_pTM": "0.84"},
            {"Design": "design2", "Average_i_pTM": "0.78"},
        ]

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_data):
            response = client.get(
                "/api/s3/csv/results/test/file.csv?columns=Design&columns=Average_i_pTM"
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["columns"] == ["Design", "Average_i_pTM"]
            assert "Rank" not in data["columns"]

    def test_read_csv_empty_file(self, client: TestClient):
        """Test reading empty CSV file."""
        with patch("app.routes.s3_files.read_csv_from_s3", return_value=[]):
            response = client.get("/api/s3/csv/results/test/file.csv")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total_rows"] == 0
            assert data["data"] == []
            assert data["columns"] == []

    def test_read_csv_configuration_error(self, client: TestClient):
        """Test reading CSV with configuration error."""
        with patch(
            "app.routes.s3_files.read_csv_from_s3",
            side_effect=S3ConfigurationError("AWS_S3_BUCKET not set"),
        ):
            response = client.get("/api/s3/csv/results/test/file.csv")

            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert "S3 configuration error" in response.json()["detail"]

    def test_read_csv_service_error(self, client: TestClient):
        """Test reading CSV with service error."""
        with patch(
            "app.routes.s3_files.read_csv_from_s3",
            side_effect=S3ServiceError("Failed to read file"),
        ):
            response = client.get("/api/s3/csv/results/test/file.csv")

            assert response.status_code == status.HTTP_502_BAD_GATEWAY
            assert "S3 service error" in response.json()["detail"]


class TestGetRunMaxScoreEndpoint:
    """Tests for GET /api/s3/run/{run_id}/max-score endpoint."""

    def test_get_max_score_success(self, client: TestClient):
        """Test successful max score calculation."""
        mock_csv_data = [
            {"Average_i_pTM": "0.84"},
            {"Average_i_pTM": "0.78"},
            {"Average_i_pTM": "0.92"},
        ]
        mock_max = 0.92

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_csv_data):
            with patch("app.routes.s3_files.calculate_csv_column_max", return_value=mock_max):
                response = client.get("/api/s3/run/test-run/max-score")

                assert response.status_code == status.HTTP_200_OK
                data = response.json()
                assert data["run_id"] == "test-run"
                assert data["max_i_ptm"] == 0.92
                assert data["total_designs"] == 3
                assert "s3://results/test-run/ranker/s1_final_design_stats.csv" in data["file_path"]

    def test_get_max_score_with_custom_parameters(self, client: TestClient):
        """Test max score with custom folder parameters."""
        mock_csv_data = [{"Average_i_pTM": "0.85"}]
        mock_max = 0.85

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_csv_data):
            with patch("app.routes.s3_files.calculate_csv_column_max", return_value=mock_max):
                response = client.get(
                    "/api/s3/run/test-run/max-score?folder_prefix=custom&subfolder=output&filename=stats.csv"
                )

                assert response.status_code == status.HTTP_200_OK
                data = response.json()
                assert "custom/test-run/output/stats.csv" in data["file_path"]

    def test_get_max_score_file_not_found(self, client: TestClient):
        """Test max score with non-existent file."""
        with patch(
            "app.routes.s3_files.read_csv_from_s3",
            side_effect=S3ServiceError("File not found"),
        ):
            response = client.get("/api/s3/run/nonexistent-run/max-score")

            assert response.status_code == status.HTTP_404_NOT_FOUND
            assert "File not found" in response.json()["detail"]

    def test_get_max_score_configuration_error(self, client: TestClient):
        """Test max score with configuration error."""
        mock_csv_data = [{"Average_i_pTM": "0.85"}]

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_csv_data):
            with patch(
                "app.routes.s3_files.calculate_csv_column_max",
                side_effect=S3ConfigurationError("AWS_S3_BUCKET not set"),
            ):
                response = client.get("/api/s3/run/test-run/max-score")

                assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
                assert "S3 configuration error" in response.json()["detail"]

    def test_get_max_score_invalid_data(self, client: TestClient):
        """Test max score with non-numeric values."""
        mock_csv_data = [{"Average_i_pTM": "0.85"}]

        with patch("app.routes.s3_files.read_csv_from_s3", return_value=mock_csv_data):
            with patch(
                "app.routes.s3_files.calculate_csv_column_max",
                side_effect=ValueError("Column contains non-numeric value"),
            ):
                response = client.get("/api/s3/run/test-run/max-score")

                assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
                assert "Invalid data" in response.json()["detail"]

    def test_get_max_score_empty_file(self, client: TestClient):
        """Test max score with empty CSV file."""
        with patch("app.routes.s3_files.read_csv_from_s3", return_value=[]):
            response = client.get("/api/s3/run/test-run/max-score")

            # Should still try to calculate max, which will fail
            # since there's no data to get max from
            # The endpoint first checks row count, so total_designs will be 0
            # But we need to mock calculate_csv_column_max to avoid actual calculation
            with patch(
                "app.routes.s3_files.calculate_csv_column_max",
                side_effect=S3ServiceError("No valid numeric values found"),
            ):
                response = client.get("/api/s3/run/test-run/max-score")
                assert response.status_code == status.HTTP_404_NOT_FOUND


class TestS3ResponseModels:
    """Tests for S3 response model validation."""

    def test_s3_file_info_model(self):
        """Test S3FileInfo model."""
        from app.routes.s3_files import S3FileInfo

        file_info = S3FileInfo(
            key="results/test/file.csv",
            size=1024,
            last_modified="2026-01-15T10:00:00Z",
            bucket="test-bucket",
        )

        assert file_info.key == "results/test/file.csv"
        assert file_info.size == 1024
        assert file_info.bucket == "test-bucket"

    def test_max_score_response_model(self):
        """Test MaxScoreResponse model."""
        from app.routes.s3_files import MaxScoreResponse

        response = MaxScoreResponse(
            run_id="test-run",
            max_i_ptm=0.92,
            total_designs=5,
            file_path="s3://results/test-run/ranker/s1_final_design_stats.csv",
        )

        assert response.run_id == "test-run"
        assert response.max_i_ptm == 0.92
        assert response.total_designs == 5
        assert "s3://" in response.file_path

    def test_csv_data_response_model(self):
        """Test CSVDataResponse model."""
        from app.routes.s3_files import CSVDataResponse

        response = CSVDataResponse(
            data=[{"col1": "val1", "col2": "val2"}],
            total_rows=1,
            columns=["col1", "col2"],
        )

        assert response.total_rows == 1
        assert len(response.data) == 1
        assert response.columns == ["col1", "col2"]
