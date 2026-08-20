"""Tests for workflow routes."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import QueuedJob
from app.db.models.core import AppUser, DataTransfer, RunInput, RunMetric, Workflow, WorkflowRun
from app.routes.dependencies import get_current_user_id, get_db
from app.services.s3 import S3UploadResult
from app.services.seqera_errors import WorkflowLaunchError

ROLES_CLAIM = "https://biocommons.org.au/roles"
WORKFLOW_ROLE = "biocommons/group/sbp_workflow_execution"


def _mock_samplesheet_staging(mock_read_csv, mock_upload_csv, field_name: str, s3_uri: str):
    """Configure read_csv_from_s3/upload_csv_to_s3 mocks for the samplesheet-file
    staging step (_stage_referenced_samplesheet_file), so a launch reaches its
    executor instead of hitting real S3 for the referenced starting_pdb/fasta file."""
    mock_read_csv.return_value = [{field_name: s3_uri}]
    mock_upload_csv.return_value = S3UploadResult(
        success=True, file_key="inputs/samplesheets/corrected.csv", bucket="test-bucket"
    )


async def _queue_job_for_route_prepare(form, **kwargs):
    db_session = kwargs["db_session"]
    workflow_run = kwargs["workflow_run"]
    queued_job = QueuedJob(
        workflow=workflow_run.workflow,
        workflow_run=workflow_run,
        launch_payload={"runName": form.runName},
        status="pending",
    )
    db_session.add(queued_job)
    db_session.flush()
    return queued_job


async def _queue_job_for_proteindj_route_prepare(form, **kwargs):
    # proteindj has no samplesheet, so prepare_proteindj_workflow takes no
    # s3_input_key positional arg (unlike prepare_bindflow_workflow above).
    db_session = kwargs["db_session"]
    workflow_run = kwargs["workflow_run"]
    queued_job = QueuedJob(
        workflow=workflow_run.workflow,
        workflow_run=workflow_run,
        launch_payload={"runName": form.runName},
        status="pending",
    )
    db_session.add(queued_job)
    db_session.flush()
    return queued_job


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
            tool="bindcraft",
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


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_success_without_dataset(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Test successful workflow launch without dataset."""
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )
    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
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
    assert data["message"] == "Workflow queued successfully"
    assert data["status"] == "staging"
    assert "submitTime" in data
    launch_form_arg = mock_prepare.call_args.args[0]
    assert launch_form_arg.tool == "bindcraft"
    assert mock_prepare.call_args.kwargs["pipeline"] == "file:/staged/workflow-repo/path"
    assert mock_prepare.call_args.kwargs["revision"] == "dev"
    assert mock_prepare.call_args.kwargs["output_id"] == data["runId"]

    with Session(test_engine) as db:
        created_run = db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.seqera_run_id,
                WorkflowRun.run_name,
                WorkflowRun.binder_name,
                WorkflowRun.sample_id,
                WorkflowRun.submitted_form_data,
                WorkflowRun.submission_timestamp,
            ).where(WorkflowRun.id == UUID(data["runId"]))
        ).first()
        assert created_run is not None
        assert created_run.seqera_run_id is None
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
        queued_job = db.scalar(select(QueuedJob).where(QueuedJob.workflow_run_id == created_run.id))
        assert queued_job is not None
        assert queued_job.status == "staging"

        run_input = db.scalar(select(RunInput).where(RunInput.run_id == created_run.id))
        assert run_input is not None
        assert run_input.data_transfer_id is not None
        input_transfer = db.scalar(
            select(DataTransfer).where(DataTransfer.id == run_input.data_transfer_id)
        )
        assert input_transfer is not None
        assert input_transfer.workflow_run_id == created_run.id
        assert input_transfer.direction == "input"
        assert input_transfer.provider == "globus"
        # The samplesheet is re-uploaded with the starting_pdb column corrected to a
        # staged Gadi path (_stage_referenced_samplesheet_file), so the main input
        # transfer's source is the corrected samplesheet key, not the original upload.
        assert input_transfer.source_location.endswith("inputs/samplesheets/corrected.csv")
        assert input_transfer.status == "pending"
        assert input_transfer.destination_location == (
            f"/test/input/de-novo-design/{created_run.id}/corrected.csv"
        )

        # The starting_pdb file referenced by the samplesheet gets its own transfer.
        pdb_transfer = db.scalar(
            select(DataTransfer).where(
                DataTransfer.workflow_run_id == created_run.id,
                DataTransfer.source_location == "s3://test-bucket/pdb/target.pdb",
            )
        )
        assert pdb_transfer is not None
        assert pdb_transfer.direction == "input"
        assert pdb_transfer.provider == "globus"
        assert pdb_transfer.destination_location == (
            f"/test/input/de-novo-design/{created_run.id}/target.pdb"
        )


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_bindcraft_fills_in_default_settings_assets_in_samplesheet(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient
):
    """settings_filters/settings_advanced samplesheet columns are left empty
    by the frontend (see sbp-portal's de-novo-design.ts) - confirmed in
    production via a real staged samplesheet. _rewrite_bindflow_settings_
    asset_columns must fill them in with the local Gadi path to bindflow's
    bundled default JSON files before the samplesheet is staged to Gadi."""
    mock_read_csv.return_value = [
        {
            "starting_pdb": "s3://test-bucket/pdb/target.pdb",
            "settings_filters": "",
            "settings_advanced": "",
        }
    ]
    mock_upload_csv.return_value = S3UploadResult(
        success=True, file_key="inputs/samplesheets/corrected.csv", bucket="test-bucket"
    )

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "test-run"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    # First call corrects starting_pdb (_stage_referenced_samplesheet_file),
    # second call rewrites the settings_* columns - both re-upload the row.
    assert mock_upload_csv.call_count == 2
    rewritten_row = mock_upload_csv.call_args_list[1].args[0]
    assert rewritten_row["settings_filters"] == (
        "/staged/workflow-repo/assets/assets/bindcraft/default_filters.json"
    )
    assert rewritten_row["settings_advanced"] == (
        "/staged/workflow-repo/assets/assets/bindcraft/default_4stage_multimer.json"
    )


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow")
def test_launch_queue_preparation_configuration_error(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Local queue payload configuration errors should return 500."""
    mock_prepare.side_effect = WorkflowLaunchError("Missing output identifier for workflow launch")
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "output identifier" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 0


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow")
def test_launch_queue_preparation_error(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Unexpected queue preparation errors are returned as local queue failures."""
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )
    mock_prepare.side_effect = RuntimeError("could not build queue payload")

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to queue local workflow run."
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.run_name == "test-run")
        )
        assert count == 0


def test_launch_de_novo_design_tool_mismatch_returns_500(client: TestClient):
    """de-novo-design is matched on tool; an unconfigured tool for it is a 500."""
    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "rfdiffusion",
            "runName": "test-run",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {"workflow": "de-novo-design", "tool": "rfdiffusion"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "rfdiffusion" in response.json()["detail"]


def _add_rfdiffusion_workflow(test_engine):
    """Helper to add a de-novo-design/rfdiffusion workflow row to the test DB."""
    with Session(test_engine) as db:
        existing = db.scalar(
            select(Workflow).where(
                Workflow.name == "de-novo-design", Workflow.tool == "rfdiffusion"
            )
        )
        if not existing:
            db.add(
                Workflow(
                    id=uuid4(),
                    name="de-novo-design",
                    tool="rfdiffusion",
                    description="ProteinDJ workflow",
                    repo_url="https://github.com/test/proteindj",
                    default_revision="dev",
                    config_path="/some/proteindj.config",
                )
            )
            db.commit()


@patch("app.routes.workflows.prepare_bindflow_workflow")
@patch(
    "app.routes.workflows.prepare_proteindj_workflow",
    side_effect=_queue_job_for_proteindj_route_prepare,
)
def test_launch_de_novo_design_rfdiffusion_routes_to_proteindj(
    mock_prepare_proteindj, mock_prepare_bindflow, client: TestClient, test_engine
):
    """tool='rfdiffusion' on de-novo-design must dispatch to the proteindj executor."""
    _add_rfdiffusion_workflow(test_engine)

    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "rfdiffusion",
            "runName": "rfd-run-1",
        },
        "s3InputKey": "inputs/pdb/target.pdb",
        "formData": {"workflow": "de-novo-design", "tool": "rfdiffusion"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "staging"
    mock_prepare_proteindj.assert_called_once()
    mock_prepare_bindflow.assert_not_called()
    assert mock_prepare_proteindj.call_args.kwargs["pipeline"] == "file:/staged/workflow-repo/path"
    assert mock_prepare_proteindj.call_args.kwargs["output_id"] == data["runId"]
    # rfdiffusion's s3InputKey is the starting PDB's own URI (no samplesheet exists
    # for it), and prepare_proteindj_workflow stages that file itself - so the
    # generic launch flow must not also create a DataTransfer for it here.
    with Session(test_engine) as db:
        transfer_count = db.scalar(
            select(func.count())
            .select_from(DataTransfer)
            .where(DataTransfer.workflow_run_id == UUID(data["runId"]))
        )
        assert transfer_count == 0


def test_launch_invalid_payload(client: TestClient):
    """Test launch with invalid payload."""
    payload = {
        "launch": {},
        "unknownField": "not allowed",
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422  # Validation error


def test_launch_rejects_missing_s3_input_key(client: TestClient):
    """s3InputKey is required; omitting it must return 422."""
    payload = {
        "launch": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "runName": "test-run",
        },
        "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422


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
        "s3InputKey": "inputs/samplesheets/test.csv",
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
        "s3InputKey": "inputs/samplesheets/test.csv",
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
    from app.schemas.workflows.shared import WorkflowFormData

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


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch(
    "app.routes.workflows.prepare_proteinfold_workflow", side_effect=_queue_job_for_route_prepare
)
def test_launch_proteinfold_success(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Test successful proteinfold workflow launch."""
    _add_proteinfold_workflow(test_engine)
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "fasta", "s3://test-bucket/fasta/seq.fasta"
    )

    payload = {
        "launch": {"workflow": "single-prediction", "tool": "colabfold", "runName": "pf-run-1"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "entities": [
                {
                    "id": "seq1",
                    "moleculeType": "protein",
                    "copyNumber": 1,
                    "sequence": "ACDEFGHIK",
                }
            ],
        },
    }

    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "staging"
    run_id = UUID(data["runId"])
    mock_prepare.assert_called_once()
    assert mock_prepare.call_args.kwargs["pipeline"] == "file:/staged/workflow-repo/path"
    assert mock_prepare.call_args.kwargs["revision"] == "dev"
    assert mock_prepare.call_args.kwargs["output_id"] == str(run_id)
    with Session(test_engine) as db:
        queued_job = db.scalar(select(QueuedJob).where(QueuedJob.workflow_run_id == run_id))
        assert queued_job is not None
        assert queued_job.status == "staging"


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_proteinfold_workflow")
def test_launch_proteinfold_queue_preparation_configuration_error(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Local queue payload configuration errors should return 500."""
    _add_proteinfold_workflow(test_engine)
    mock_prepare.side_effect = WorkflowLaunchError("Missing run name for workflow launch")
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "fasta", "s3://test-bucket/fasta/seq.fasta"
    )

    payload = {
        "launch": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "runName": "pf-run-cfg-err",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "entities": [
                {
                    "id": "seq1",
                    "moleculeType": "protein",
                    "copyNumber": 1,
                    "sequence": "ACDEFGHIK",
                }
            ],
        },
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert "run name" in response.json()["detail"]


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_proteinfold_workflow")
def test_launch_proteinfold_queue_preparation_error(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    """Unexpected queue preparation errors should return 500."""
    _add_proteinfold_workflow(test_engine)
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "fasta", "s3://test-bucket/fasta/seq.fasta"
    )
    mock_prepare.side_effect = RuntimeError("queue build failed")

    payload = {
        "launch": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "runName": "pf-run-exec-err",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "single-prediction",
            "tool": "colabfold",
            "entities": [
                {
                    "id": "seq1",
                    "moleculeType": "protein",
                    "copyNumber": 1,
                    "sequence": "ACDEFGHIK",
                }
            ],
        },
    }

    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to queue local workflow run."


# =============================================================================
# Tests for single-prediction entity validation
# =============================================================================


def _single_prediction_payload(entities, tool="colabfold", **form_extra):
    form_data = {"workflow": "single-prediction", "tool": tool, "entities": entities}
    form_data.update(form_extra)
    return {
        "launch": {"workflow": "single-prediction", "tool": tool, "runName": "sp-val"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": form_data,
    }


def _protein_entity(sequence="ACDEFGHIK", copy_number=1):
    return {
        "id": "seq1",
        "moleculeType": "protein",
        "copyNumber": copy_number,
        "sequence": sequence,
    }


def test_launch_single_prediction_rejects_missing_entities(client: TestClient, test_engine):
    _add_proteinfold_workflow(test_engine)
    payload = {
        "launch": {"workflow": "single-prediction", "tool": "colabfold", "runName": "sp-no-ent"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {"workflow": "single-prediction", "tool": "colabfold"},
    }
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 422
    assert "entities" in response.json()["detail"]


def test_launch_single_prediction_rejects_too_many_entities(client: TestClient, test_engine):
    _add_proteinfold_workflow(test_engine)
    payload = _single_prediction_payload([_protein_entity(copy_number=53)])
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 422
    assert "Too many entities" in response.json()["detail"]


def test_launch_single_prediction_requires_protein(client: TestClient, test_engine):
    _add_proteinfold_workflow(test_engine)
    entities = [
        {"id": "seq1", "moleculeType": "dna", "copyNumber": 1, "sequence": "ACGT"},
    ]
    payload = _single_prediction_payload(entities, tool="boltz")
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 422
    assert "must be a protein" in response.json()["detail"]


def test_launch_single_prediction_rejects_oversized_alphafold2(client: TestClient, test_engine):
    _add_proteinfold_workflow(test_engine)
    payload = _single_prediction_payload([_protein_entity(sequence="A" * 2000)], tool="alphafold2")
    response = client.post("/api/workflows/launch", json=payload)
    assert response.status_code == 422
    assert "less than 2000" in response.json()["detail"]


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch(
    "app.routes.workflows.prepare_proteinfold_workflow", side_effect=_queue_job_for_route_prepare
)
def test_launch_single_prediction_boltz_potentials_reduces_limit(
    mock_prepare, mock_read_csv, mock_upload_csv, client: TestClient, test_engine
):
    _add_proteinfold_workflow(test_engine)
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "fasta", "s3://test-bucket/fasta/seq.fasta"
    )

    ok_payload = _single_prediction_payload(
        [_protein_entity(sequence="A" * 1999)], tool="boltz", boltz_use_potentials=True
    )
    assert client.post("/api/workflows/launch", json=ok_payload).status_code == 201

    over_payload = _single_prediction_payload(
        [_protein_entity(sequence="A" * 2000)], tool="boltz", boltz_use_potentials=True
    )
    response = client.post("/api/workflows/launch", json=over_payload)
    assert response.status_code == 422
    assert "less than 2000" in response.json()["detail"]
    mock_prepare.assert_called_once()


# =============================================================================
# Tests for require_workflow_execution_role
# =============================================================================


_LAUNCH_PAYLOAD = {
    "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "role-test-run"},
    "s3InputKey": "inputs/samplesheets/test.csv",
    "formData": {"workflow": "de-novo-design", "tool": "bindcraft"},
}


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_allowed_with_workflow_role(
    mock_prepare, mock_read_csv, mock_upload_csv, role_check_client, monkeypatch
):
    """Users holding the workflow execution role can launch."""
    monkeypatch.setenv("DB_ADMIN_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("WORKFLOW_EXECUTION_ROLE", WORKFLOW_ROLE)
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )

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
    assert response.json()["status"] == "staging"
    mock_prepare.assert_called_once()


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


def test_create_app_fails_when_workflow_env_vars_unset(monkeypatch, mocker, test_get_settings):
    """create_app() raises ValidationError when required workflow env vars are missing."""
    monkeypatch.delenv("AUTH_WORKFLOW_EXECUTION_ROLE")
    mocker.patch("app.main.get_settings", test_get_settings)
    with pytest.raises(ValidationError, match="workflow_execution_role"):
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


@patch("app.routes.workflows.prepare_wisps_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_interaction_screening_success(mock_prepare, wisps_client: TestClient, test_engine):
    """Test successful interaction-screening workflow launch."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
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
    assert data["status"] == "staging"
    run_id = UUID(data["runId"])
    mock_prepare.assert_called_once()
    call_kwargs = mock_prepare.call_args.kwargs
    assert call_kwargs["form_data"].fastaS3Uri == "s3://bucket/test.fasta"
    assert call_kwargs["form_data"].splitOutputDir == "/data/split"
    assert call_kwargs["pipeline"] == "file:/staged/workflow-repo/path"
    assert call_kwargs["revision"] in {"dev", "main"}
    assert call_kwargs["output_id"] == str(run_id)

    with Session(test_engine) as db:
        created_run = db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.seqera_run_id,
                WorkflowRun.run_name,
                WorkflowRun.submitted_form_data,
                WorkflowRun.submission_timestamp,
            ).where(WorkflowRun.id == run_id)
        ).first()
        assert created_run is not None
        assert created_run.seqera_run_id is None
        assert created_run.run_name == "wisps-run"
        assert created_run.submitted_form_data["fastaS3Uri"] == "s3://bucket/test.fasta"
        assert created_run.submitted_form_data["splitOutputDir"] == "/data/split"
        assert created_run.submission_timestamp is not None
        queued_job = db.scalar(select(QueuedJob).where(QueuedJob.workflow_run_id == created_run.id))
        assert queued_job is not None
        assert queued_job.status == "staging"

        # Both the samplesheet and the aggregated FASTA it references must be
        # staged - _notify_launcher only flips "staging" -> "pending" once every
        # input DataTransfer for this run is completed.
        transfers = db.scalars(
            select(DataTransfer).where(DataTransfer.workflow_run_id == created_run.id)
        ).all()
        assert len(transfers) == 2
        sources = {t.source_location for t in transfers}
        assert sources == {
            "s3://test-s3-bucket/inputs/samplesheets/test.csv",
            "s3://bucket/test.fasta",
        }


def test_launch_interaction_screening_missing_fasta(wisps_client: TestClient):
    """Missing fastaS3Uri in formData should return 422."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
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
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 422
    assert "splitOutputDir" in response.json()["detail"]


@patch("app.routes.workflows.prepare_wisps_workflow")
def test_launch_interaction_screening_queue_preparation_configuration_error(
    mock_prepare, wisps_client: TestClient, test_engine
):
    """Local queue payload configuration errors should return 500."""
    mock_prepare.side_effect = WorkflowLaunchError("Missing output identifier for workflow launch")

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-cfg-err",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert "output identifier" in response.json()["detail"]
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.run_name == "wisps-run-cfg-err")
        )
        assert count == 0


@patch("app.routes.workflows.prepare_wisps_workflow")
def test_launch_interaction_screening_queue_preparation_error(
    mock_prepare, wisps_client: TestClient, test_engine
):
    """Unexpected queue preparation errors should return 500."""
    mock_prepare.side_effect = RuntimeError("queue build failed")

    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-exec-err",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to queue local workflow run."
    with Session(test_engine) as db:
        count = db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.run_name == "wisps-run-exec-err")
        )
        assert count == 0


@patch("app.routes.workflows.prepare_wisps_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_with_workflow_field_in_launch(mock_prepare, wisps_client: TestClient, test_engine):
    """The new frontend format using launch.workflow is accepted alongside launch.tool."""
    payload = {
        "launch": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "runName": "wisps-run-workflow-field",
        },
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "interaction-screening",
            "tool": "boltz",
            "fastaS3Uri": "s3://bucket/test.fasta",
            "splitOutputDir": "/data/split",
        },
    }

    response = wisps_client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    run_id = UUID(response.json()["runId"])
    assert response.json()["status"] == "staging"
    mock_prepare.assert_called_once()

    with Session(test_engine) as db:
        created_run = db.execute(
            select(WorkflowRun.run_name).where(WorkflowRun.id == run_id)
        ).first()
        assert created_run is not None
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
    assert de_novo["toolMultipliers"] == {"bindcraft": 10, "rfdiffusion": 4}

    single = by_category["single-prediction"]
    assert single["basis"] == CreditBasis.CONSTANT.value
    assert single["toolMultipliers"] == {"boltz": 50, "colabfold": 50, "alphafold2": 200}

    bulk = by_category["bulk-prediction"]
    assert bulk["basis"] == CreditBasis.FASTA_ENTRY_COUNT.value
    assert bulk["toolMultipliers"] == {"boltz": 1, "colabfold": 3}

    screening = by_category["interaction-screening"]
    assert screening["basis"] == CreditBasis.FASTA_PAIR_PRODUCT.value
    assert screening["toolMultipliers"] == {"boltz": 1, "colabfold": 5}


# ── Server-side credit deduction at launch ───────────────────────────────────

TEST_USER_ID = UUID("11111111-1111-1111-1111-111111111111")


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_deducts_credits_when_enabled(
    mock_prepare,
    mock_read_csv,
    mock_upload_csv,
    client,
    test_engine,
    monkeypatch,
    mock_settings,
):
    """With credits enabled, a successful de-novo launch deducts multiplier × designs."""
    mock_settings.enable_credits = True
    client.app.dependency_overrides[get_settings] = lambda: mock_settings
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=100))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "credit-run"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 3,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    assert response.json()["status"] == "staging"
    mock_prepare.assert_called_once()
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 70  # 100 − (10 × 3)


@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_rejected_when_insufficient_credits(
    mock_prepare, client, test_engine, monkeypatch, mock_settings
):
    """With credits enabled, an unaffordable launch is rejected (402) and not queued."""
    monkeypatch.setenv("ENABLE_CREDITS", "true")
    mock_settings.enable_credits = True
    client.app.dependency_overrides[get_settings] = lambda: mock_settings
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=10))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "credit-run"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 3,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 402
    mock_prepare.assert_not_called()
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 10  # unchanged


@patch("app.routes.workflows.upload_csv_to_s3")
@patch("app.routes.workflows.read_csv_from_s3")
@patch("app.routes.workflows.prepare_bindflow_workflow", side_effect=_queue_job_for_route_prepare)
def test_launch_does_not_deduct_when_credits_disabled(
    mock_prepare, mock_read_csv, mock_upload_csv, client, test_engine, monkeypatch
):
    """With credits disabled (default), launches never touch the balance."""
    monkeypatch.delenv("ENABLE_CREDITS", raising=False)
    _mock_samplesheet_staging(
        mock_read_csv, mock_upload_csv, "starting_pdb", "s3://test-bucket/pdb/target.pdb"
    )
    with Session(test_engine) as db:
        db.execute(update(AppUser).where(AppUser.id == TEST_USER_ID).values(credit=5))
        db.commit()

    payload = {
        "launch": {"workflow": "de-novo-design", "tool": "bindcraft", "runName": "nocredit-run"},
        "s3InputKey": "inputs/samplesheets/test.csv",
        "formData": {
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "id": "s1",
            "number_of_final_designs": 999,
        },
    }
    response = client.post("/api/workflows/launch", json=payload)

    assert response.status_code == 201
    assert response.json()["status"] == "staging"
    mock_prepare.assert_called_once()
    with Session(test_engine) as db:
        credit = db.scalar(select(AppUser.credit).where(AppUser.id == TEST_USER_ID))
    assert credit == 5  # unchanged
