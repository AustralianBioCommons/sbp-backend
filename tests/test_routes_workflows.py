"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.seqera import (
    SeqeraConfigurationError,
    SeqeraLaunchResult,
    SeqeraServiceError,
)


class TestLaunchWorkflow:
    """Tests for POST /api/workflows/launch endpoint."""

    @patch("app.routes.workflows.launch_seqera_workflow")
    async def test_launch_success_without_dataset(self, mock_launch, client: TestClient):
        """Test successful workflow launch without dataset."""
        mock_launch.return_value = SeqeraLaunchResult(
            workflow_id="wf_123",
            status="submitted",
            message="Success",
        )

        payload = {
            "launch": {
                "pipeline": "https://github.com/test/repo",
                "runName": "test-run",
            }
        }

        response = client.post("/api/workflows/launch", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["runId"] == "wf_123"
        assert data["status"] == "submitted"
        assert "submitTime" in data

    @patch("app.routes.workflows.upload_dataset_to_seqera")
    @patch("app.routes.workflows.create_seqera_dataset")
    @patch("app.routes.workflows.launch_seqera_workflow")
    async def test_launch_success_with_form_data(
        self, mock_launch, mock_create_dataset, mock_upload, client: TestClient
    ):
        """Test successful workflow launch with form data."""
        # Mock dataset creation
        mock_create_result = AsyncMock()
        mock_create_result.dataset_id = "dataset_456"
        mock_create_dataset.return_value = mock_create_result

        # Mock dataset upload
        mock_upload.return_value = None

        # Mock workflow launch
        mock_launch.return_value = SeqeraLaunchResult(
            workflow_id="wf_789",
            status="submitted",
        )

        payload = {
            "launch": {
                "pipeline": "https://github.com/test/repo",
                "runName": "test-with-data",
            },
            "formData": {
                "sample": "test",
                "input": "/path/file.txt",
            },
        }

        response = client.post("/api/workflows/launch", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["runId"] == "wf_789"

        # Verify dataset creation was called
        mock_create_dataset.assert_called_once()
        mock_upload.assert_called_once()

    @patch("app.routes.workflows.launch_seqera_workflow")
    async def test_launch_configuration_error(self, mock_launch, client: TestClient):
        """Test launch with configuration error."""
        mock_launch.side_effect = SeqeraConfigurationError("Missing API token")

        payload = {
            "launch": {
                "pipeline": "https://github.com/test/repo",
            }
        }

        response = client.post("/api/workflows/launch", json=payload)

        assert response.status_code == 500
        assert "Missing API token" in response.json()["detail"]

    @patch("app.routes.workflows.launch_seqera_workflow")
    async def test_launch_service_error(self, mock_launch, client: TestClient):
        """Test launch with Seqera service error."""
        mock_launch.side_effect = SeqeraServiceError("API returned 502")

        payload = {
            "launch": {
                "pipeline": "https://github.com/test/repo",
            }
        }

        response = client.post("/api/workflows/launch", json=payload)

        assert response.status_code == 502
        assert "API returned 502" in response.json()["detail"]

    def test_launch_invalid_payload(self, client: TestClient):
        """Test launch with invalid payload."""
        payload = {
            "launch": {
                "pipeline": "",  # Empty pipeline
            }
        }

        response = client.post("/api/workflows/launch", json=payload)

        assert response.status_code == 422  # Validation error


class TestCancelWorkflow:
    """Tests for POST /api/workflows/{run_id}/cancel endpoint."""

    def test_cancel_workflow_success(self, client: TestClient):
        """Test successful workflow cancellation."""
        response = client.post("/api/workflows/run_123/cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["runId"] == "run_123"
        assert data["status"] == "cancelled"
        assert "message" in data


class TestListRuns:
    """Tests for GET /api/workflows/runs endpoint."""

    def test_list_runs_default_params(self, client: TestClient):
        """Test listing runs with default parameters."""
        response = client.get("/api/workflows/runs")

        assert response.status_code == 200
        data = response.json()
        assert "runs" in data
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert data["total"] == 0

    def test_list_runs_with_filters(self, client: TestClient):
        """Test listing runs with filter parameters."""
        response = client.get(
            "/api/workflows/runs",
            params={
                "status": "running",
                "workspace": "test_ws",
                "limit": 10,
                "offset": 5,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 5

    def test_list_runs_limit_validation(self, client: TestClient):
        """Test that limit must be between 1 and 200."""
        # Test limit too high
        response = client.get("/api/workflows/runs", params={"limit": 300})
        assert response.status_code == 422

        # Test limit too low
        response = client.get("/api/workflows/runs", params={"limit": 0})
        assert response.status_code == 422

    def test_list_runs_offset_validation(self, client: TestClient):
        """Test that offset must be non-negative."""
        response = client.get("/api/workflows/runs", params={"offset": -1})
        assert response.status_code == 422


class TestGetLogs:
    """Tests for GET /api/workflows/{run_id}/logs endpoint."""

    def test_get_logs_success(self, client: TestClient):
        """Test successful log retrieval."""
        response = client.get("/api/workflows/run_123/logs")

        assert response.status_code == 200
        data = response.json()
        assert "entries" in data
        assert "truncated" in data
        assert "pending" in data
        assert isinstance(data["entries"], list)


class TestGetDetails:
    """Tests for GET /api/workflows/{run_id}/details endpoint."""

    def test_get_details_success(self, client: TestClient):
        """Test successful details retrieval."""
        response = client.get("/api/workflows/run_123/details")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "run_123"
        assert "status" in data
        assert "runName" in data
