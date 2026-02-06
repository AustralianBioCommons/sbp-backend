"""Shared test fixtures and configuration."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from polyfactory.factories.pydantic_factory import ModelFactory

# Set test environment variables before importing app
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,http://localhost:4200"
os.environ["SEQERA_API_URL"] = "https://api.seqera.test"
os.environ["SEQERA_ACCESS_TOKEN"] = "test_token_12345"
os.environ["WORK_SPACE"] = "test_workspace_id"
os.environ["COMPUTE_ID"] = "test_compute_env_id"
os.environ["WORK_DIR"] = "/test/work/dir"
os.environ["AWS_S3_BUCKET"] = "test-s3-bucket"

from app.main import create_app
from app.schemas.workflows import (
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    RunInfo,
    WorkflowLaunchForm,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
)

# ============================================================================
# Auto-generate test data from Pydantic schemas
# ============================================================================


class WorkflowLaunchFormFactory(ModelFactory[WorkflowLaunchForm]):
    """Factory for generating WorkflowLaunchForm test data."""

    __model__ = WorkflowLaunchForm
    __check_model__ = False


class WorkflowLaunchPayloadFactory(ModelFactory[WorkflowLaunchPayload]):
    """Factory for generating WorkflowLaunchPayload test data."""

    __model__ = WorkflowLaunchPayload
    __check_model__ = False


class WorkflowLaunchResponseFactory(ModelFactory[WorkflowLaunchResponse]):
    """Factory for generating WorkflowLaunchResponse test data."""

    __model__ = WorkflowLaunchResponse
    __check_model__ = False


class RunInfoFactory(ModelFactory[RunInfo]):
    """Factory for generating RunInfo test data."""

    __model__ = RunInfo
    __check_model__ = False


class ListRunsResponseFactory(ModelFactory[ListRunsResponse]):
    """Factory for generating ListRunsResponse test data."""

    __model__ = ListRunsResponse
    __check_model__ = False


class LaunchLogsFactory(ModelFactory[LaunchLogs]):
    """Factory for generating LaunchLogs test data."""

    __model__ = LaunchLogs
    __check_model__ = False


class LaunchDetailsFactory(ModelFactory[LaunchDetails]):
    """Factory for generating LaunchDetails test data."""

    __model__ = LaunchDetails
    __check_model__ = False


# ============================================================================
# Database Test Fixtures
# ============================================================================


@pytest.fixture
def test_engine():
    """Create a test database engine using SQLite in-memory."""
    from sqlalchemy import create_engine

    from app.db import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def test_db(test_engine) -> Generator:
    """Create a test database session."""
    from sqlalchemy.orm import sessionmaker

    test_session_local = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)
    session = test_session_local()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ============================================================================
# FastAPI Test Clients
# ============================================================================


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
def sample_workflow_launch_form():
    """Sample workflow launch form data.

    NOTE: Consider using WorkflowLaunchFormFactory.build() directly in tests.
    """
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
