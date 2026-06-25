"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models.core import AppUser, RunMetric, Workflow, WorkflowRun
from app.routes.dependencies import get_current_user_id, get_db
from app.services.seqera import WorkflowExecutorError, WorkflowLaunchResult
from app.services.seqera_errors import SeqeraConfigurationError

ROLES_CLAIM = "https://biocommons.org.au/roles"
WORKFLOW_ROLE = "biocommons/group/sbp_workflow_execution"


@pytest.fixture
def role_check_client(test_engine):
    """Test client with auth bypassed but require_workflow_execution_role active."""
    from app.main import create_app

    application = create_app()
    user_id = UUID("22222222-2222-2222-2222-222222222222")

    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(
        bind=test_engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    setup_session = SessionLocal()
    if not setup_session.get(AppUser, user_id):
        setup_session.add(
            AppUser(
                id=user_id,
                auth0_user_id="auth0|role-test",
                name="Role User",
                email="role@example.com",
            )
        )
    setup_session.add(
        Workflow(
            id=uuid4(),
            name="de-novo-design",
            description="Test workflow",
            repo_url="https://github.com/test/repo",
            default_revision="dev",
            config_path="/some/bindflow.config",
            prerun_script_path="/some/bindflow-prerun.sh",
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

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_current_user_id] = lambda: user_id
    with TestClient(application) as c:
        yield c


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_without_dataset(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch without dataset."""
    mock_launch.return_value = WorkflowLaunchResult(
        workflow_id="wf_123",
        status="submitted",
        message="Success",
    )

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "binder_name": "PDL1",
            "number_of_final_designs": 20,
        },
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wf_123"
    assert data["status"] == "submitted"
    assert "submitTime" in data
    launch_form_arg = mock_launch.call_args[0][0]
    assert launch_form_arg.tool == "bindcraft"
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
                WorkflowRun.submission_timestamp,
            ).where(WorkflowRun.seqera_run_id == "wf_123")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_123"
        assert created_run.run_name == "test-run"
        assert created_run.binder_name == "PDL1"
        assert created_run.sample_id == "s1"
        # submitted_form_data may include Pydantic default fields; check that all
        # submitted fields are present rather than exact equality.
        for key, value in payload["formData"].items():
            assert created_run.submitted_form_data[key] == value
        assert created_run.submission_timestamp is not None
        metric = db.execute(
            select(RunMetric).where(RunMetric.run_id == created_run.id)
        ).scalar_one()
        assert metric.final_design_count == 20


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_success_with_dataset_id(mock_launch, client: TestClient, test_engine):
    """Test successful workflow launch with pre-created dataset ID."""
    mock_launch.return_value = WorkflowLaunchResult(
        workflow_id="wf_789",
        status="submitted",
    )

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-with-data",
        },
        "datasetId": "dataset_456",  # Use existing dataset
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
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
    mock_launch.side_effect = SeqeraConfigurationError("Missing API token")

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
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
    mock_launch.side_effect = WorkflowExecutorError("API returned 502")

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
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
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "datasetId": "   ",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422
    assert "datasetId is required" in response.json()["detail"]


def test_cancel_workflow_endpoint_removed(client: TestClient):
    """Cancel endpoint is intentionally removed from jobs API."""
    response = client.post("/api/workflows/run_123/cancel")
    assert response.status_code == 404


def test_launch_rejects_workflow_not_in_db(client: TestClient):
    """A valid workflow name with no DB entry returns 500 not configured."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"workflow": "interaction-screening", "tool": "boltz"},
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_launch_rejects_invalid_workflow_schema(client: TestClient):
    """An unknown workflow name that fails schema validation returns 422."""
    payload = {
        "launch": {
            "workflow": "unknown-workflow",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "datasetId": "dataset_123",
        "formData": {"workflow": "unknown-workflow", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 422


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
    response = client.get(
        "/api/workflows/runs", params={"status": "RUNNING", "limit": 10, "offset": 5}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["runs"] == []
    assert data["total"] == 0
    assert data["limit"] == 10
    assert data["offset"] == 5


# =============================================================================
# Tests for _extract_form_id()
# =============================================================================


def _form_data(**extra):
    from app.schemas.workflows import WorkflowFormData

    return WorkflowFormData(workflow="de-novo-design", tool="bindcraft", **extra)


def test_extract_form_id_none_input():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(None) is None


def test_extract_form_id_not_workflowformdata():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id("not a WorkflowFormData") is None  # type: ignore[arg-type]
    assert _extract_sample_id(42) is None  # type: ignore[arg-type]


def test_extract_form_id_missing_keys():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data()) is None


def test_extract_form_id_empty_string_value():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data(samplesheetId=" ", id="  ", sample_id="")) is None


def test_extract_form_id_prefers_sample_id():
    from app.routes.workflows import _extract_sample_id

    assert (
        _extract_sample_id(
            _form_data(sample_id="sample-001", samplesheetId="sample-sheet-001", id="id-001")
        )
        == "sample-001"
    )


def test_extract_form_id_uses_id_key():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data(id="sample_001")) == "sample_001"


def test_extract_form_id_falls_back_to_sample_id():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data(sample_id="s_002")) == "s_002"


def test_extract_form_id_falls_back_to_samplesheet_id():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data(samplesheetId="sheet-002")) == "sheet-002"


def test_extract_form_id_strips_whitespace():
    from app.routes.workflows import _extract_sample_id

    assert _extract_sample_id(_form_data(id="  s1  ")) == "s1"


# =============================================================================
# Tests for _extract_binder_name()
# =============================================================================


def test_extract_binder_name_none_input():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(None) is None


def test_extract_binder_name_not_workflowformdata():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name("not a WorkflowFormData") is None  # type: ignore[arg-type]


def test_extract_binder_name_missing_key():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(_form_data()) is None


def test_extract_binder_name_blank_value():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(_form_data(binder_name="  ")) is None


def test_extract_binder_name_valid():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(_form_data(binder_name="PDL1")) == "PDL1"


def test_extract_binder_name_strips_whitespace():
    from app.routes.workflows import _extract_binder_name

    assert _extract_binder_name(_form_data(binder_name="  CTLA4  ")) == "CTLA4"


# =============================================================================
# Tests for _extract_final_design_count()
# =============================================================================


def test_extract_final_design_count_none_input():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(None) is None


def test_extract_final_design_count_not_workflowformdata():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count("not a WorkflowFormData") is None  # type: ignore[arg-type]


def test_extract_final_design_count_missing_key():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data()) is None


def test_extract_final_design_count_invalid_string():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs="not_a_number")) is None


def test_extract_final_design_count_negative():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs=-5)) is None


def test_extract_final_design_count_zero():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs=0)) is None


def test_extract_final_design_count_valid():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs=10)) == 10


def test_extract_final_design_count_one():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs=1)) == 1


def test_extract_final_design_count_string_number():
    from app.routes.workflows import _extract_final_design_count

    assert _extract_final_design_count(_form_data(number_of_final_designs="25")) == 25


# =============================================================================
# Tests for missing repo_url / default_revision
# =============================================================================


def test_launch_missing_repo_url(client: TestClient, app, test_engine):
    """Workflow missing repo_url should return 500."""
    with Session(test_engine) as db:
        db.add(
            Workflow(
                id=uuid4(),
                name="single-prediction",
                description="No repo workflow",
                repo_url=None,
                default_revision="dev",
            )
        )
        db.commit()

    payload = {
        "launch": {"workflow": "single-prediction", "tool": "colabfold", "runName": "test-run"},
        "datasetId": "dataset_123",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
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
                name="single-prediction",
                description="No revision workflow",
                repo_url="https://github.com/test/norev",
                default_revision=None,
            )
        )
        db.commit()

    payload = {
        "launch": {"workflow": "single-prediction", "tool": "colabfold", "runName": "test-run"},
        "datasetId": "dataset_123",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
    }
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "missing default_revision" in response.json()["detail"]


# =============================================================================
# Tests for proteinfold launch path
# =============================================================================


def _add_proteinfold_workflow(test_engine):
    """Helper to add a single-prediction workflow to the test DB."""
    with Session(test_engine) as db:
        existing = db.scalar(select(Workflow).where(Workflow.name == "single-prediction"))
        if not existing:
            db.add(
                Workflow(
                    id=uuid4(),
                    name="single-prediction",
                    description="Single prediction workflow",
                    repo_url="https://github.com/nf-core/proteinfold",
                    default_revision="dev",
                    config_path="/some/proteinfold.config",
                    prerun_script_path="/some/proteinfold-prerun.sh",
                )
            )
            db.commit()


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_success(mock_launch, client: TestClient, test_engine):
    """Test successful proteinfold workflow launch."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.return_value = WorkflowLaunchResult(
        workflow_id="pf_wf_001",
        status="submitted",
        message=None,
    )

    payload = {
        "launch": {"workflow": "single-prediction", "tool": "colabfold", "runName": "pf-run-1"},
        "datasetId": "dataset_pf",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "pf_wf_001"
    assert data["status"] == "submitted"
    mock_launch.assert_called_once()
    assert mock_launch.call_args.kwargs["prerun_script_path"] == "/some/proteinfold-prerun.sh"


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_configuration_error(mock_launch, client: TestClient, test_engine):
    """SeqeraConfigurationError should return 500."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.side_effect = SeqeraConfigurationError("Missing SEQERA_API_URL")

    payload = {
        "launch": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "runName": "pf-run-cfg-err",
        },
        "datasetId": "dataset_pf",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "Missing SEQERA_API_URL" in response.json()["detail"]


@patch("app.routes.workflows.launch_proteinfold_workflow")
def test_launch_proteinfold_executor_error(mock_launch, client: TestClient, test_engine):
    """WorkflowExecutorError should return 502."""
    _add_proteinfold_workflow(test_engine)
    mock_launch.side_effect = WorkflowExecutorError("Seqera API 503")

    payload = {
        "launch": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "runName": "pf-run-exec-err",
        },
        "datasetId": "dataset_pf",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 502
    assert "Seqera API 503" in response.json()["detail"]


# =============================================================================
# Tests for require_workflow_execution_role
# =============================================================================


_LAUNCH_PAYLOAD = {
    "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "role-test-run"},
    "datasetId": "dataset_role",
    "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
}


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_allowed_with_workflow_role(mock_launch, role_check_client, monkeypatch):
    """Users holding the workflow execution role can launch."""
    monkeypatch.setenv("DB_ADMIN_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("WORKFLOW_EXECUTION_ROLE", WORKFLOW_ROLE)
    mock_launch.return_value = WorkflowLaunchResult(workflow_id="wf_role_ok", status="submitted")

    with patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={ROLES_CLAIM: [WORKFLOW_ROLE]},
    ):
        response = role_check_client.post(
            "/api/workflows/launch",
            json=_LAUNCH_PAYLOAD,
            headers={"Authorization": "Bearer mock-token"},
        )

    assert response.status_code == 201


def test_launch_denied_without_workflow_role(role_check_client, monkeypatch):
    """Users without the workflow execution role receive HTTP 403."""
    monkeypatch.setenv("DB_ADMIN_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("WORKFLOW_EXECUTION_ROLE", WORKFLOW_ROLE)

    with patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={ROLES_CLAIM: ["biocommons/group/other"]},
    ):
        response = role_check_client.post(
            "/api/workflows/launch",
            json=_LAUNCH_PAYLOAD,
            headers={"Authorization": "Bearer mock-token"},
        )

    assert response.status_code == 403
    assert "Workflow execution role required" in response.json()["detail"]


def test_create_app_fails_when_workflow_env_vars_unset(monkeypatch):
    """create_app() raises RuntimeError when required workflow env vars are missing."""
    monkeypatch.delenv("WORKFLOW_EXECUTION_ROLE")
    with pytest.raises(RuntimeError, match="WORKFLOW_EXECUTION_ROLE"):
        from app.main import create_app

        create_app()


# =============================================================================
# Fixtures and tests for interaction-screening (WISPS) launch path
# =============================================================================


@pytest.fixture
def wisps_client(test_engine):
    """Test client with both BindCraft and interaction-screening workflows in the DB."""
    from sqlalchemy.orm import sessionmaker

    from app.main import create_app

    application = create_app()
    user_id = UUID("11111111-1111-1111-1111-111111111111")

    SessionLocal = sessionmaker(
        bind=test_engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    setup_session = SessionLocal()

    if not setup_session.get(AppUser, user_id):
        setup_session.add(
            AppUser(
                id=user_id,
                auth0_user_id="auth0|test-user",
                name="Test User",
                email="test@example.com",
            )
        )

    from sqlalchemy import select as sa_select

    existing_bc = setup_session.scalar(sa_select(Workflow).where(Workflow.name == "BindCraft"))
    if not existing_bc:
        setup_session.add(
            Workflow(
                id=uuid4(),
                name="BindCraft",
                description="Test BindCraft workflow",
                repo_url="https://github.com/test/repo",
                default_revision="dev",
                config_path="/some/bindflow.config",
            )
        )

    existing_wisps = setup_session.scalar(
        sa_select(Workflow).where(Workflow.name == "interaction-screening")
    )
    if not existing_wisps:
        setup_session.add(
            Workflow(
                id=uuid4(),
                name="interaction-screening",
                description="WISPS interaction screening workflow",
                repo_url="https://github.com/test/wisps",
                default_revision="main",
                config_path="/some/config.nf",
                prerun_script_path="/some/wisps-prerun.sh",
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

    from app.routes.dependencies import require_workflow_execution_role

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_current_user_id] = lambda: user_id
    application.dependency_overrides[require_workflow_execution_role] = lambda: None

    with TestClient(application) as c:
        yield c


@pytest.fixture
def wisps_no_config_client(test_engine):
    """Test client with an interaction-screening workflow that has config_path=None."""
    from sqlalchemy.orm import sessionmaker

    from app.main import create_app

    application = create_app()
    user_id = UUID("11111111-1111-1111-1111-111111111111")

    SessionLocal = sessionmaker(
        bind=test_engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    setup_session = SessionLocal()

    if not setup_session.get(AppUser, user_id):
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
            name="interaction-screening",
            description="WISPS workflow without config_path",
            repo_url="https://github.com/test/wisps",
            default_revision="main",
            config_path=None,
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

    from app.routes.dependencies import require_workflow_execution_role

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_current_user_id] = lambda: user_id
    application.dependency_overrides[require_workflow_execution_role] = lambda: None

    with TestClient(application) as c:
        yield c


@patch("app.routes.workflows.launch_wisps_workflow")
def test_launch_interaction_screening_success(mock_wisps, wisps_client: TestClient, test_engine):
    """Test successful interaction-screening workflow launch."""
    mock_wisps.return_value = WorkflowLaunchResult(
        workflow_id="wisps_wf_001",
        status="submitted",
    )

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["runId"] == "wisps_wf_001"
    mock_wisps.assert_called_once()
    call_kwargs = mock_wisps.call_args.kwargs
    assert call_kwargs["form_data"].fastaS3Uri == "s3://bucket/test.fasta"
    assert call_kwargs["form_data"].splitOutputDir == "/data/split"
    assert call_kwargs["prerun_script_path"] == "/some/wisps-prerun.sh"

    with Session(test_engine) as db:
        created_run = db.execute(
            select(
                WorkflowRun.seqera_dataset_id,
                WorkflowRun.run_name,
                WorkflowRun.submitted_form_data,
                WorkflowRun.submission_timestamp,
            ).where(WorkflowRun.seqera_run_id == "wisps_wf_001")
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_wisps"
        assert created_run.run_name == "wisps-run"
        assert created_run.submitted_form_data["fastaS3Uri"] == "s3://bucket/test.fasta"
        assert created_run.submitted_form_data["splitOutputDir"] == "/data/split"
        assert created_run.submission_timestamp is not None


def test_launch_interaction_screening_missing_fasta(wisps_client: TestClient):
    """Missing fastaS3Uri in formData should return 422."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422
    assert "fastaS3Uri" in response.json()["detail"]


def test_launch_interaction_screening_missing_split_output_dir(wisps_client: TestClient):
    """Missing splitOutputDir in formData should return 422."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422
    assert "splitOutputDir" in response.json()["detail"]


@patch("app.routes.workflows.launch_wisps_workflow")
def test_launch_interaction_screening_config_error(
    mock_wisps, wisps_client: TestClient, test_engine
):
    """SeqeraConfigurationError should return 500."""
    mock_wisps.side_effect = SeqeraConfigurationError("missing token")

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-cfg-err",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "missing token" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.run_name == "wisps-run-cfg-err")
        )
        assert count == 1


@patch("app.routes.workflows.launch_wisps_workflow")
def test_launch_interaction_screening_executor_error(
    mock_wisps, wisps_client: TestClient, test_engine
):
    """WorkflowExecutorError should return 502."""
    mock_wisps.side_effect = WorkflowExecutorError("seqera 502")

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-exec-err",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 502
    assert "seqera 502" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.run_name == "wisps-run-exec-err")
        )
        assert count == 1


def test_launch_interaction_screening_missing_config_path(wisps_no_config_client: TestClient):
    """interaction-screening workflow missing config_path should return 500."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-no-cfg",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_no_config_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "config_path" in response.json()["detail"]


@patch("app.routes.workflows.launch_wisps_workflow")
def test_launch_with_workflow_field_in_launch(mock_wisps, wisps_client: TestClient, test_engine):
    """The new frontend format using launch.workflow is accepted alongside launch.tool."""
    mock_wisps.return_value = WorkflowLaunchResult(
        workflow_id="wisps_wf_002",
        status="submitted",
    )

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-workflow-field",
        },
        "datasetId": "dataset_wisps",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    assert response.json()["runId"] == "wisps_wf_002"

    with Session(test_engine) as db:
        created_run = db.execute(
            select(WorkflowRun.seqera_dataset_id, WorkflowRun.run_name).where(
                WorkflowRun.seqera_run_id == "wisps_wf_002"
            )
        ).first()
        assert created_run is not None
        assert created_run.seqera_dataset_id == "dataset_wisps"
        assert created_run.run_name == "wisps-run-workflow-field"


# Tests for GET /api/workflows/credits
# =============================================================================


def test_get_workflow_credits_returns_all_categories(client: TestClient):
    """The credits endpoint returns the cost rules for every workflow category."""
    response = client.get("/api/workflows/credits")

    assert response.status_code == 200
    workflows = response.json()["workflows"]
    by_category = {wf["category"]: wf for wf in workflows}

    assert set(by_category) == {
        "de-novo-design",
        "single-prediction",
        "bulk-prediction",
        "interaction-screening",
    }


def test_get_workflow_credits_multipliers_match_spec(client: TestClient):
    """Tool multipliers and cost basis match the SBP credit-calculation spec."""
    from app.services.credits import CreditBasis

    response = client.get("/api/workflows/credits")
    assert response.status_code == 200
    by_category = {wf["category"]: wf for wf in response.json()["workflows"]}

    de_novo = by_category["de-novo-design"]
    assert de_novo["basis"] == CreditBasis.FINAL_DESIGN_COUNT.value
    assert de_novo["toolMultipliers"] == {"bindcraft": 20, "rfdiffusion": 10}

    single = by_category["single-prediction"]
    assert single["basis"] == CreditBasis.CONSTANT.value
    assert single["toolMultipliers"] == {"boltz": 1, "colabfold": 5, "alphafold2": 5}

    bulk = by_category["bulk-prediction"]
    assert bulk["basis"] == CreditBasis.FASTA_ENTRY_COUNT.value
    assert bulk["toolMultipliers"] == {"boltz": 1, "colabfold": 1}

    screening = by_category["interaction-screening"]
    assert screening["basis"] == CreditBasis.FASTA_PAIR_PRODUCT.value
    assert screening["toolMultipliers"] == {"boltz": 1, "colabfold": 1}


# ── Server-side credit deduction at launch ───────────────────────────────────

TEST_USER_ID = UUID("11111111-1111-1111-1111-111111111111")


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_deducts_credits_when_enabled(mock_launch, client, test_engine, monkeypatch):
    """With credits enabled, a successful de-novo launch deducts multiplier × designs."""
    monkeypatch.setenv("ENABLE_CREDITS", "true")
    mock_launch.return_value = WorkflowLaunchResult(workflow_id="wf_credit", status="submitted")
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=100))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "credit-run"},
        "datasetId": "dataset_123",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 3,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 40  # 100 − (20 × 3)


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_rejected_when_insufficient_credits(mock_launch, client, test_engine, monkeypatch):
    """With credits enabled, an unaffordable launch is rejected (402) and not launched."""
    monkeypatch.setenv("ENABLE_CREDITS", "true")
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=10))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "credit-run"},
        "datasetId": "dataset_123",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 3,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 402
    mock_launch.assert_not_called()
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 10  # unchanged


@patch("app.routes.workflows.launch_bindflow_workflow")
def test_launch_does_not_deduct_when_credits_disabled(
    mock_launch, client, test_engine, monkeypatch
):
    """With credits disabled (default), launches never touch the balance."""
    monkeypatch.delenv("ENABLE_CREDITS", raising=False)
    mock_launch.return_value = WorkflowLaunchResult(workflow_id="wf_nocredit", status="submitted")
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=5))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "nocredit-run"},
        "datasetId": "dataset_123",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 999,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 5  # unchanged
