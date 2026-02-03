"""Tests for job listing and details endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes.dependencies import get_current_user_id, get_db


class _DummyResult:
    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _DummyDB:
    def execute(self, *_args, **_kwargs):
        return _DummyResult()

    def commit(self):
        return None

    def rollback(self):
        return None

    def delete(self, *_args, **_kwargs):
        return None


@pytest.fixture
def client():
    app = create_app()

    def override_get_current_user_id() -> UUID:
        return UUID("11111111-1111-1111-1111-111111111111")

    def override_get_db():
        yield _DummyDB()

    app.dependency_overrides[get_current_user_id] = override_get_current_user_id
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_list_jobs_endpoint_success(client):
    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value={"wf-123", "wf-456"}),
        patch(
            "app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={"wf-123": 0.953}
        ),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=object()),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=0.953,
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=[
                {
                    "workflow": {
                        "id": "wf-123",
                        "runName": "Job A",
                        "projectName": "BindCraft",
                        "status": "SUCCEEDED",
                        "submit": "2026-02-01T10:00:00Z",
                    }
                },
                {
                    "workflow": {
                        "id": "wf-456",
                        "runName": "Job B",
                        "projectName": "De novo design",
                        "status": "RUNNING",
                        "submit": "2026-02-02T10:00:00Z",
                    }
                },
            ],
        ),
    ):
        response = client.get("/api/workflows/jobs?limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["jobs"]) == 2
    assert data["jobs"][1]["score"] == 0.953


def test_list_jobs_with_search_and_status_filter(client):
    with (
        patch("app.routes.workflow.jobs.get_owned_run_ids", return_value={"wf-1", "wf-2"}),
        patch("app.routes.workflow.jobs.get_score_by_seqera_run_id", return_value={}),
        patch("app.routes.workflow.jobs.get_owned_run", return_value=object()),
        patch(
            "app.routes.workflow.jobs.ensure_completed_run_score",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routes.workflow.jobs.describe_workflow",
            new_callable=AsyncMock,
            side_effect=[
                {
                    "workflow": {
                        "id": "wf-1",
                        "runName": "Matching Job",
                        "projectName": "BindCraft",
                        "status": "SUCCEEDED",
                        "submit": "2026-02-01T10:00:00Z",
                    }
                },
                {
                    "workflow": {
                        "id": "wf-2",
                        "runName": "Other Job",
                        "projectName": "BindCraft",
                        "status": "FAILED",
                        "submit": "2026-02-01T11:00:00Z",
                    }
                },
            ],
        ),
    ):
        response = client.get(
            "/api/workflows/jobs?search=Matching&status=Completed&limit=10&offset=0"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["jobs"][0]["jobName"] == "Matching Job"


def test_list_jobs_invalid_limit(client):
    response = client.get("/api/workflows/jobs?limit=0")
    assert response.status_code == 422


def test_list_jobs_invalid_offset(client):
    response = client.get("/api/workflows/jobs?offset=-1")
    assert response.status_code == 422


def test_list_jobs_configuration_error(client):
    from app.services.seqera import SeqeraConfigurationError

    with patch(
        "app.routes.workflow.jobs.describe_workflow",
        new_callable=AsyncMock,
        side_effect=SeqeraConfigurationError(
            "Missing required environment variable: SEQERA_API_URL"
        ),
    ), patch("app.routes.workflow.jobs.get_owned_run_ids", return_value={"wf-123"}):
        response = client.get("/api/workflows/jobs?limit=10&offset=0")

    assert response.status_code == 500


def test_list_jobs_api_error(client):
    from app.services.seqera import SeqeraAPIError

    with patch(
        "app.routes.workflow.jobs.describe_workflow",
        new_callable=AsyncMock,
        side_effect=SeqeraAPIError("Seqera API is down"),
    ), patch("app.routes.workflow.jobs.get_owned_run_ids", return_value={"wf-123"}):
        response = client.get("/api/workflows/jobs?limit=10&offset=0")

    assert response.status_code == 502
    assert "Seqera API is down" in response.json()["detail"]
