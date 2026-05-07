"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.core import RunMetric, Workflow, WorkflowRun
from app.services.bindflow_executor import (
    BindflowConfigurationError,
    BindflowExecutorError,
    BindflowLaunchResult,
)
from app.services.proteinfold_executor import (
    ProteinfoldConfigurationError,
    ProteinfoldExecutorError,
    ProteinfoldLaunchResult,
)


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_without_dataset(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch without dataset."""
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_123",
        status="submitted",
        message="Success",
    )

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"id": "s1", "binder_name": "PDL1", "number_of_final_designs": 20},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_123"
    assert data["status"] == "submitted"
    assert "submitTime" in data
    launch_form_arg = mock_launch.call_args[0][0]
    assert launch_form_arg.tool == "BindCraft"
    assert mock_launch.call_args.kwargs["pipeline"] == "https://github.com/test/repo"
    assert mock_launch.call_args.kwargs["revision"] == "dev"
    assert isinstance(mock_launch.call_args.kwargs["output_id"], str)

    with Session(test_engine) as db:
        created_run = db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.seqera_dataset_id,
                WorkflowRun.run_name,
                WorkflowRun.binder_name,
                WorkflowRun.sample_id,
                WorkflowRun.submitted_form_data,
            ).where(WorkflowRun.seqera_run_id == "wf_123")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_123"
        assert created_run.run_name == "test-run"
        assert created_run.binder_name == "PDL1"
        assert created_run.sample_id == "s1"
        assert created_run.submitted_form_data == payload["formData"]
        metric = db.execute(
            select(RunMetric).where(RunMetric.run_id == created_run.id)
        ).scalar_one()
        assert metric.final_design_count == 20


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_with_dataset_id(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch with pre-created dataset ID."""
    mock_launch.return_value = BindflowLaunchResult(
        workflow_id="wf_789",
        status="submitted",
    )

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-with-data",
        },
        "datasetId": "dataset_456",  # Use existing dataset
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_789"

    mock_launch.assert_called_once()
    call_args = mock_launch.call_args
    assert call_args[0][1] == "dataset_456"

    with Session(test_engine) as db:
        created_run = db.execute(
            select(WorkflowRun.seqera_dataset_id).where(WorkflowRun.seqera_run_id == "wf_789")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_456"


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_configuration_error(mock_launch, client: TestClient, test_engine):
    """Test launch with configuration error."""
    mock_launch.side_effect = BindflowConfigurationError("Missing API token")

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "Missing API token" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 1


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_service_error(mock_launch, client: TestClient, test_engine):
    """Test launch with Seqera service error."""
    mock_launch.side_effect = BindflowExecutorError("API returned 502")

    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 502
    assert "API returned 502" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 1


def test_launch_invalid_payload(client: TestClient):
    """Test launch with invalid payload."""
    payload = {
        "launch": {},
        "unknownField": "not allowed",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422  # Validation error


def test_launch_rejects_blank_dataset_id(client: TestClient):
    """datasetId must be non-empty after trimming."""
    payload = {
        "launch": {
            "tool": "BindCraft",
            "runName": "test-run",
        },
        "datasetId": "   ",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422
    assert "datasetId is required" in response.json()["detail"]


def test_cancel_workflow_endpoint_removed(client: TestClient):
    """Cancel endpoint is intentionally removed from jobs API."""
    response = client.post("/api/workflows/run_123/cancel")
    assert response.status_code == 404


def test_launch_rejects_unavailable_tool(client: TestClient):
    payload = {
        "launch": {
            "tool": "BoltzGen",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_launch_rejects_unknown_tool(client: TestClient):
    payload = {
        "launch": {
            "tool": "UnknownTool",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_get_logs_success(client: TestClient):
    """Test successful log retrieval."""
    response = client.get("/api/workflows/run_123/logs")

    assert response.status_code == 200
    data = response.json()
    assert "entries" in data
    assert "truncated" in data
    assert "pending" in data
    assert isinstance(data["entries"], list)


def test_get_details_success(client: TestClient):
    """Test successful details retrieval."""
    response = client.get("/api/workflows/run_123/details")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "run_123"
    assert "status" in data
    assert "runName" in data


def test_list_runs_placeholder(client: TestClient):
    """List runs currently returns an empty placeholder response."""
    response = client.get("/api/workflows/runs", params={"status": "RUNNING", "limit": 10, "offset": 5})

    assert response.status_code == 200
    data = response.json()
    assert data["runs"] == []
    assert data["total"] == 0
    assert data["limit"] == 10
    assert data["offset"] == 5


# =============================================================================
# Tests for _extract_form_id()
# =============================================================================


def test_extract_form_id_none_input():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id(None) is None


def test_extract_form_id_not_dict():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id("not a dict") is None  # type: ignore[arg-type]
    assert _extract_form_id(42) is None  # type: ignore[arg-type]


def test_extract_form_id_missing_keys():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id({"unrelated_key": "value"}) is None


def test_extract_form_id_empty_string_value():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id({"id": "  ", "sample_id": ""}) is None


def test_extract_form_id_uses_id_key():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id({"id": "sample_001"}) == "sample_001"


def test_extract_form_id_falls_back_to_sample_id():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id({"sample_id": "s_002"}) == "s_002"


def test_extract_form_id_strips_whitespace():
    from app.routes.workflows import _extract_form_id

    assert _extract_form_id({"id": "  s1  "}) == "s1"


# =============================================================================
# Tests for _extract_binder_name()
# =============================================================================


def test_extract_binder_name_none_input():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(None) is None


def test_extract_binder_name_not_dict():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name("not a dict") is None  # type: ignore[arg-type]


def test_extract_binder_name_missing_key():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name({"other_key": "value"}) is None


def test_extract_binder_name_blank_value():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name({"binder_name": "  "}) is None


def test_extract_binder_name_valid():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name({"binder_name": "PDL1"}) == "PDL1"


def test_extract_binder_name_strips_whitespace():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name({"binder_name": "  CTLA4  "}) == "CTLA4"


# =============================================================================
# Tests for _extract_final_design_count()
# =============================================================================


def test_extract_final_design_count_none_input():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(None) is None


def test_extract_final_design_count_not_dict():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count("not a dict") is None  # type: ignore[arg-type]


def test_extract_final_design_count_missing_key():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"other_key": 5}) is None


def test_extract_final_design_count_invalid_string():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": "not_a_number"}) is None


def test_extract_final_design_count_negative():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": -5}) is None


def test_extract_final_design_count_zero():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": 0}) is None


def test_extract_final_design_count_valid():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": 10}) == 10


def test_extract_final_design_count_one():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": 1}) == 1


def test_extract_final_design_count_string_number():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count({"number_of_final_designs": "25"}) == 25


# =============================================================================
# Tests for missing repo_url / default_revision
# =============================================================================


def test_launch_missing_repo_url(client: TestClient, app, test_engine):
    """Workflow missing repo_url should return 500."""
    with Session(test_engine) as db:
        db.add(
            Workflow(
                id=uuid4(),
                name="norepo",
                description="No repo workflow",
                repo_url=None,
                default_revision="dev",
            )
        )
        db.commit()

    payload = {
        "launch": {"tool": "norepo", "runName": "test-run"},
        "datasetId": "dataset_123",
    }
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "missing repo_url" in response.json()["detail"]


def test_launch_missing_default_revision(client: TestClient, app, test_engine):
    """Workflow missing default_revision should return 500."""
    with Session(test_engine) as db:
        db.add(
            Workflow(
                id=uuid4(),
                name="norev",
                description="No revision workflow",
                repo_url="https://github.com/test/norev",
                default_revision=None,
            )
        )
        db.commit()

    payload = {
        "launch": {"tool": "norev", "runName": "test-run"},
        "datasetId": "dataset_123",
    }
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "missing default_revision" in response.json()["detail"]


# =============================================================================
# Tests for proteinfold launch path
# =============================================================================


def _add_proteinfold_workflow(test_engine):
    """Helper to add a proteinfold workflow to the test DB."""
    with Session(test_engine) as db:
        existing = db.scalar(select(Workflow).where(Workflow.name == "proteinfold"))
        if not existing:
            db.add(
                Workflow(
                    id=uuid4(),
                    name="proteinfold",
                    description="Proteinfold workflow",
                    repo_url="https://github.com/nf-core/proteinfold",
                    default_revision="dev",
                )
            )
            db.commit()


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_success(mock_launch, client: TestClient, test_engine):
    """Test successful proteinfold workflow launch."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.return_value = ProteinfoldLaunchResult(
        workflow_id="pf_wf_001",
        status="submitted",
        message=None,
    )

    payload = {
        "launch": {"tool": "proteinfold", "runName": "pf-run-1"},
        "datasetId": "dataset_pf",
        "formData": {"mode": "alphafold2", "seqeraRunName": "pf-run-1"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "pf_wf_001"
    assert data["status"] == "submitted"
    mock_launch.assert_called_once()


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_configuration_error(mock_launch, client: TestClient, test_engine):
    """ProteinfoldConfigurationError should return 500."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.side_effect = ProteinfoldConfigurationError("Missing SEQERA_API_URL")

    payload = {
        "launch": {"tool": "proteinfold", "runName": "pf-run-cfg-err"},
        "datasetId": "dataset_pf",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "Missing SEQERA_API_URL" in response.json()["detail"]


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_executor_error(mock_launch, client: TestClient, test_engine):
    """ProteinfoldExecutorError should return 502."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.side_effect = ProteinfoldExecutorError("Seqera API 503")

    payload = {
        "launch": {"tool": "proteinfold", "runName": "pf-run-exec-err"},
        "datasetId": "dataset_pf",
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 502
    assert "Seqera API 503" in response.json()["detail"]
