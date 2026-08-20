"""Shared test fixtures and configuration."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import Field

from app.config import (
    AdminSettings,
    AuthSettings,
    AwsSettings,
    GlobusSettings,
    SeqeraSettings,
    Settings,
    get_settings,
)

# Set test environment variables before importing app
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,http://localhost:4200"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PORT"] = "8000"
os.environ["SEQERA_API_URL"] = "https://api.seqera.test"
os.environ["SEQERA_ACCESS_TOKEN"] = "test_token_12345"
os.environ["SEQERA_WORK_SPACE"] = "test_workspace_id"
os.environ["SEQERA_COMPUTE_ID"] = "test_compute_env_id"
os.environ["SEQERA_WORK_DIR"] = "/test/work/dir"
os.environ["AWS_ACCESS_KEY_ID"] = "test_access_key_id"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test_secret_access_key"
os.environ["AWS_REGION"] = "ap-southeast-2"
os.environ["AWS_S3_BUCKET"] = "test-s3-bucket"
os.environ["GLOBUS_CLIENT_ID"] = "test-globus-client-id"
os.environ["GLOBUS_CLIENT_SECRET"] = "test-globus-client-secret"
os.environ["GLOBUS_GADI_COLLECTION_ID"] = "test-gadi-collection-id"
os.environ["GLOBUS_GADI_COLLECTION_ROOT"] = "/test"
os.environ["GLOBUS_S3_COLLECTION_ID"] = "test-s3-collection-id"
os.environ["GLOBUS_INPUT_DIR"] = "/test/input"
os.environ["GLOBUS_OUTPUT_DIR"] = "/test/output"
os.environ["ENABLE_DB_ADMIN"] = "false"
os.environ["DB_ADMIN_AUTH_REDIRECT_URI"] = "http://localhost:3000/admin/login"
os.environ["DB_ADMIN_FORBIDDEN_HOME_URL"] = "http://localhost:3000"
os.environ["DB_ADMIN_COOKIE_SECURE"] = "false"
os.environ["DB_ADMIN_SESSION_SECRET"] = "test-session-secret"
os.environ["DB_ADMIN_ROLES_CLAIM"] = "https://biocommons.org.au/roles"
os.environ["AUTH_DOMAIN"] = "example.auth.test"
os.environ["AUTH_CLIENT_ID"] = "test-client-id"
os.environ["AUTH_AUDIENCE"] = "https://example.api.test"
os.environ["AUTH_REDIRECT_URI"] = "http://localhost:3000/auth/callback"
os.environ["AUTH_REQUIRED_ROLE"] = "biocommons/group/sbp_admin"
os.environ["AUTH_WORKFLOW_EXECUTION_ROLE"] = "biocommons/group/sbp_workflow_execution"
os.environ["WORKFLOW_EXECUTION_ROLE"] = "biocommons/group/sbp_workflow_execution"

from uuid import UUID, uuid4

from app.db.models.core import AppUser, Workflow
from app.main import app as fastapi_app
from app.main import create_app
from app.routes.dependencies import get_current_user_id, get_db, require_workflow_execution_role
from app.schemas.workflows.shared import (
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
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


@pytest.fixture
def persistent_models(test_db):
    """Bind datagen SQLAlchemy factories to the test DB session."""
    from tests import datagen

    factories = [
        datagen.AppUserFactory,
        datagen.WorkflowFactory,
        datagen.WorkflowRunFactory,
        datagen.RunInputFactory,
        datagen.RunOutputFactory,
        datagen.S3ObjectFactory,
        datagen.QueuedJobFactory,
        datagen.DataTransferFactory,
    ]

    for factory in factories:
        factory.__session__ = test_db

    try:
        yield
    finally:
        for factory in factories:
            factory.__session__ = None


# ============================================================================
# FastAPI Test Clients
# ============================================================================


@pytest.fixture
def app(test_engine):
    """Create a FastAPI app instance for testing."""
    app = create_app()
    user_id = UUID("11111111-1111-1111-1111-111111111111")

    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(
        bind=test_engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    setup_session = SessionLocal()
    setup_session.add(
        AppUser(
            id=user_id,
            auth0_user_id="auth0|test-user",
            name="Test User",
            email="test@example.com",
        )
    )
    setup_session.add(
        Workflow(
            id=uuid4(),
            name="de-novo-design",
            tool="bindcraft",
            description="Test workflow",
            repo_url="https://github.com/test/repo",
            default_revision="dev",
            config_path="/some/bindflow.config",
        )
    )
    setup_session.commit()
    setup_session.close()

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[require_workflow_execution_role] = lambda: None
    return app


@pytest.fixture
def client(app) -> Generator[TestClient]:
    """Create a test client for the FastAPI app."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(app) -> AsyncGenerator[AsyncClient]:
    """Create an async test client for the FastAPI app."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_workflow_launch_form():
    """Sample workflow launch form data.

    NOTE: Consider using WorkflowLaunchFormFactory.build() directly in tests.
    """
    return {
        "workflow": "de-novo-design",
        "tool": "bindcraft",
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


class SeqeraSettingsNoEnv(SeqeraSettings):
    model_config = {**SeqeraSettings.model_config, "env_file": None}


class AwsSettingsNoEnv(AwsSettings):
    model_config = {**AwsSettings.model_config, "env_file": None}


class AdminSettingsNoEnv(AdminSettings):
    model_config = {**AdminSettings.model_config, "env_file": None}


class AuthSettingsNoEnv(AuthSettings):
    model_config = {**AuthSettings.model_config, "env_file": None}


class GlobusSettingsNoEnv(GlobusSettings):
    model_config = {**GlobusSettings.model_config, "env_file": None}


class SettingsNoEnv(Settings):
    """
    Settings class that ignores any .env files for testing
    """

    model_config = {**Settings.model_config, "env_file": None}
    seqera: SeqeraSettings = Field(default_factory=SeqeraSettingsNoEnv)
    aws: AwsSettings = Field(default_factory=AwsSettingsNoEnv)
    admin: AdminSettings = Field(default_factory=AdminSettingsNoEnv)
    auth: AuthSettings = Field(default_factory=AuthSettingsNoEnv)
    globus: GlobusSettings = Field(default_factory=GlobusSettingsNoEnv)


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    return SettingsNoEnv(database_url="sqlite:///:memory:")


@pytest.fixture(autouse=True)
def override_settings(mock_settings):
    fastapi_app.dependency_overrides[get_settings] = lambda: mock_settings
    yield
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_repo_staging(mocker):
    """launch_workflow resolves+stages the workflow's GitHub repo on every call
    (workflow_repo_staging.ensure_repo_staging_requested), which makes a real
    GitHub API request - stub it out by default so tests don't hit the network.
    Tests that specifically exercise repo staging behavior can override this
    with their own patch of the same target."""
    from app.services.workflow_repo_staging import RepoStagingLocations

    return mocker.patch(
        "app.routes.workflows.ensure_repo_staging_requested",
        return_value=RepoStagingLocations(
            gadi_path="/staged/workflow-repo/path",
            assets_gadi_path="/staged/workflow-repo/assets",
        ),
    )


@pytest.fixture
def test_get_settings():
    """
    Version of get_settings that doesn't use .env file,
    doesn't cache
    """

    def get_settings_no_env() -> SettingsNoEnv:
        return SettingsNoEnv()

    return get_settings_no_env
