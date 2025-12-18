"""Shared test fixtures and configuration."""
from __future__ import annotations

import os
from typing import AsyncGenerator, Dict, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

# Set test environment variables before importing app
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,http://localhost:4200"
os.environ["SEQERA_API_URL"] = "https://api.seqera.test"
os.environ["SEQERA_ACCESS_TOKEN"] = "test_token_12345"
os.environ["WORK_SPACE"] = "test_workspace_id"
os.environ["COMPUTE_ID"] = "test_compute_env_id"
os.environ["WORK_DIR"] = "/test/work/dir"

from app.main import create_app


@pytest.fixture
def app():
    """Create a FastAPI app instance for testing."""
    return create_app()


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    """Create a test client for the FastAPI app."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(app) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client for the FastAPI app."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_httpx_response():
    """Create a mock httpx Response."""
    def _create_response(
        status_code: int = 200,
        json_data: Dict | None = None,
        text: str = "",
        is_error: bool = False,
    ):
        response = MagicMock()
        response.status_code = status_code
        response.is_error = is_error
        response.text = text
        if json_data:
            response.json.return_value = json_data
        return response
    return _create_response


@pytest.fixture
def mock_async_client(mock_httpx_response):
    """Create a mock async HTTP client."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.get = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    return mock_client


@pytest.fixture
def sample_workflow_launch_form():
    """Sample workflow launch form data."""
    return {
        "pipeline": "https://github.com/nextflow-io/hello",
        "revision": "main",
        "configProfiles": ["singularity"],
        "runName": "test-workflow-run",
        "paramsText": "test_param: value",
    }


@pytest.fixture
def sample_form_data():
    """Sample form data for dataset creation."""
    return {
        "sample_name": "test_sample",
        "input_file": "/path/to/file.txt",
        "parameter1": "value1",
        "parameter2": 42,
    }


@pytest.fixture
def sample_seqera_dataset_response():
    """Sample Seqera dataset creation response."""
    return {
        "id": "dataset_123abc",
        "name": "test-dataset",
        "description": "Test dataset",
        "workspaceId": "test_workspace_id",
    }


@pytest.fixture
def sample_seqera_launch_response():
    """Sample Seqera workflow launch response."""
    return {
        "workflowId": "workflow_xyz789",
        "status": "submitted",
    }
