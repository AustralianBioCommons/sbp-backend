"""Tests for database models and initialization."""

from __future__ import annotations

import os

from sqlalchemy import inspect

from app.db import Base, SessionLocal, _get_database_url, engine
from app.db.models import (
    AppUser,
    RunInput,
    RunMetric,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)


def test_get_database_url_with_env():
    """Test _get_database_url returns DATABASE_URL from environment."""
    expected_url = "postgresql+psycopg://test:test@testhost:5432/testdb"
    os.environ["DATABASE_URL"] = expected_url
    result = _get_database_url()
    assert result == expected_url


def test_get_database_url_default():
    """Test _get_database_url returns default when env var not set."""
    # Save current value
    current_url = os.environ.get("DATABASE_URL")

    # Remove env var if it exists
    if "DATABASE_URL" in os.environ:
        del os.environ["DATABASE_URL"]

    result = _get_database_url()
    assert result.startswith("postgresql+psycopg://postgres:postgres@localhost:")
    assert result.endswith("/sbp")

    # Restore original value
    if current_url:
        os.environ["DATABASE_URL"] = current_url


def test_base_class():
    """Test Base declarative class is properly configured."""
    assert Base is not None
    assert hasattr(Base, "metadata")
    assert hasattr(Base, "registry")


def test_engine_creation():
    """Test engine is created with proper configuration."""
    assert engine is not None
    assert engine.pool is not None
    # Check pool_pre_ping is enabled
    assert engine.pool._pre_ping is True


def test_session_local_creation():
    """Test SessionLocal sessionmaker is configured correctly."""
    assert SessionLocal is not None
    session = SessionLocal()
    assert session is not None
    # Check session configuration
    assert hasattr(session, "bind")
    session.close()


def test_app_user_model():
    """Test AppUser model structure and relationships."""
    # Check table name
    assert AppUser.__tablename__ == "app_users"

    # Check columns exist
    mapper = inspect(AppUser)
    column_names = {col.key for col in mapper.columns}
    assert "id" in column_names
    assert "auth0_user_id" in column_names
    assert "name" in column_names
    assert "email" in column_names

    # Check relationships
    assert hasattr(AppUser, "workflow_runs")


def test_workflow_model():
    """Test Workflow model structure and relationships."""
    assert Workflow.__tablename__ == "workflows"

    mapper = inspect(Workflow)
    column_names = {col.key for col in mapper.columns}
    assert "id" in column_names
    assert "name" in column_names
    assert "description" in column_names
    assert "repo_url" in column_names
    assert "default_revision" in column_names

    assert hasattr(Workflow, "runs")


def test_workflow_run_model():
    """Test WorkflowRun model structure and relationships."""
    assert WorkflowRun.__tablename__ == "workflow_runs"

    mapper = inspect(WorkflowRun)
    column_names = {col.key for col in mapper.columns}
    assert "id" in column_names
    assert "workflow_id" in column_names
    assert "owner_user_id" in column_names
    assert "seqera_dataset_id" in column_names
    assert "seqera_run_id" in column_names
    assert "run_name" in column_names
    assert "work_dir" in column_names

    # Check relationships
    assert hasattr(WorkflowRun, "owner")
    assert hasattr(WorkflowRun, "workflow")
    assert hasattr(WorkflowRun, "metrics")
    assert hasattr(WorkflowRun, "inputs")
    assert hasattr(WorkflowRun, "outputs")

    # Check unique constraints
    constraints = list(WorkflowRun.__table__.constraints)
    constraint_names = {c.name for c in constraints}
    assert "uq_workflow_runs_seqera_run_id" in constraint_names
    assert "uq_workflow_runs_work_dir" in constraint_names


def test_s3_object_model():
    """Test S3Object model structure and relationships."""
    assert S3Object.__tablename__ == "s3_objects"

    mapper = inspect(S3Object)
    column_names = {col.key for col in mapper.columns}
    assert "object_key" in column_names
    # The column is named "URI" but accessed as "uri"
    # Check for the actual column name in table
    table_column_names = {col.name for col in S3Object.__table__.columns}
    assert "URI" in table_column_names
    assert "version_id" in column_names
    assert "size_bytes" in column_names

    assert hasattr(S3Object, "run_inputs")
    assert hasattr(S3Object, "run_outputs")

    # Check unique constraint
    constraints = list(S3Object.__table__.constraints)
    constraint_names = {c.name for c in constraints}
    assert "uq_s3_objects_URI" in constraint_names


def test_run_input_model():
    """Test RunInput model structure and relationships."""
    assert RunInput.__tablename__ == "run_inputs"

    mapper = inspect(RunInput)
    column_names = {col.key for col in mapper.columns}
    assert "run_id" in column_names
    assert "s3_object_id" in column_names

    assert hasattr(RunInput, "run")
    assert hasattr(RunInput, "s3_object")

    # Check composite primary key
    primary_keys = [col.name for col in RunInput.__table__.primary_key.columns]
    assert "run_id" in primary_keys
    assert "s3_object_id" in primary_keys


def test_run_output_model():
    """Test RunOutput model structure and relationships."""
    assert RunOutput.__tablename__ == "run_outputs"

    mapper = inspect(RunOutput)
    column_names = {col.key for col in mapper.columns}
    assert "run_id" in column_names
    assert "s3_object_id" in column_names

    assert hasattr(RunOutput, "run")
    assert hasattr(RunOutput, "s3_object")

    # Check composite primary key
    primary_keys = [col.name for col in RunOutput.__table__.primary_key.columns]
    assert "run_id" in primary_keys
    assert "s3_object_id" in primary_keys


def test_run_metric_model():
    """Test RunMetric model structure and relationships."""
    assert RunMetric.__tablename__ == "run_metrics"

    mapper = inspect(RunMetric)
    column_names = {col.key for col in mapper.columns}
    assert "run_id" in column_names
    assert "max_score" in column_names

    assert hasattr(RunMetric, "run")

    # Check primary key
    primary_keys = [col.name for col in RunMetric.__table__.primary_key.columns]
    assert "run_id" in primary_keys


def test_models_are_importable():
    """Test that all models can be imported from the models package."""
    from app.db.models import (
        AppUser as ImportedAppUser,
    )
    from app.db.models import (
        RunInput as ImportedRunInput,
    )
    from app.db.models import (
        RunMetric as ImportedRunMetric,
    )
    from app.db.models import (
        RunOutput as ImportedRunOutput,
    )
    from app.db.models import (
        S3Object as ImportedS3Object,
    )
    from app.db.models import (
        Workflow as ImportedWorkflow,
    )
    from app.db.models import (
        WorkflowRun as ImportedWorkflowRun,
    )

    assert ImportedAppUser is AppUser
    assert ImportedWorkflow is Workflow
    assert ImportedWorkflowRun is WorkflowRun
    assert ImportedS3Object is S3Object
    assert ImportedRunInput is RunInput
    assert ImportedRunOutput is RunOutput
    assert ImportedRunMetric is RunMetric


def test_model_type_annotations():
    """Test that models have proper type annotations for Mapped columns."""
    # AppUser
    assert hasattr(AppUser, "__annotations__")
    assert "id" in AppUser.__annotations__
    assert "auth0_user_id" in AppUser.__annotations__

    # Workflow
    assert "name" in Workflow.__annotations__
    assert "description" in Workflow.__annotations__

    # WorkflowRun
    assert "seqera_run_id" in WorkflowRun.__annotations__
    assert "work_dir" in WorkflowRun.__annotations__

    # S3Object
    assert "object_key" in S3Object.__annotations__
    assert "uri" in S3Object.__annotations__

    # RunMetric
    assert "max_score" in RunMetric.__annotations__
