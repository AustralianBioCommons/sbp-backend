"""Tests for Pydantic schemas."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.workflows.interaction_screening import WispsDatasetUploadRequest, WispsSequenceItem
from app.schemas.workflows.shared import (
    CancelWorkflowResponse,
    DatasetUploadRequest,
    JobListItem,
    JobListResponse,
    LaunchDetails,
    LaunchLogs,
    ListRunsResponse,
    PipelineStatus,
    RunInfo,
    UIStatus,
    WorkflowLaunchForm,
    WorkflowLaunchPayload,
    WorkflowLaunchResponse,
    WorkflowUserDetails,
    map_pipeline_status_to_ui,
)
from app.schemas.workflows.single_prediction import (
    SinglePredictionEntity,
    single_prediction_size_limit,
    validate_single_prediction_entities,
)


def test_valid_minimal_form():
    """Test WorkflowLaunchForm with minimal valid data."""
    form = WorkflowLaunchForm(workflow="de-novo-design", tool="bindcraft")

    assert form.workflow == "de-novo-design"
    assert form.tool == "bindcraft"
    assert form.configProfiles == []
    assert form.runName is None
    assert form.paramsText is None


def test_valid_complete_form():
    """Test WorkflowLaunchForm with all fields."""
    form = WorkflowLaunchForm(
        workflow="de-novo-design",
        tool="bindcraft",
        configProfiles=["docker", "test"],
        runName="my-test-run",
        paramsText="param1: value1\nparam2: value2",
    )

    assert form.workflow == "de-novo-design"
    assert form.tool == "bindcraft"
    assert form.configProfiles == ["docker", "test"]
    assert form.runName == "my-test-run"
    assert "param1" in form.paramsText


def test_tool_required_and_not_empty():
    """Test that workflow and tool are required and cannot be empty."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm()
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(workflow="de-novo-design", tool="")
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(workflow="", tool="bindcraft")


def test_tool_must_be_exact_literal():
    """Literal types don't accept whitespace-padded values."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(workflow="  de-novo-design  ", tool="bindcraft")
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(workflow="de-novo-design", tool="  bindcraft  ")


def test_extra_fields_forbidden():
    """Test that extra fields are not allowed."""
    with pytest.raises(ValidationError):
        WorkflowLaunchForm(workflow="de-novo-design", tool="bindcraft", extraField="not allowed")


def test_valid_payload_minimal():
    """Test payload with required launch, formData, and s3InputKey."""
    payload = WorkflowLaunchPayload(
        launch={"workflow": "de-novo-design", "tool": "bindcraft"},
        formData={"workflow": "de-novo-design", "tool": "bindcraft"},
        s3InputKey="inputs/samplesheets/test.csv",
    )

    assert payload.launch.workflow == "de-novo-design"
    assert payload.launch.tool == "bindcraft"
    assert payload.s3InputKey == "inputs/samplesheets/test.csv"


def test_valid_payload_with_s3_input_key():
    """Test payload with s3InputKey."""
    payload = WorkflowLaunchPayload(
        launch={"workflow": "de-novo-design", "tool": "bindcraft"},
        formData={"workflow": "de-novo-design", "tool": "bindcraft"},
        s3InputKey="inputs/samplesheets/test.csv",
    )

    assert payload.s3InputKey == "inputs/samplesheets/test.csv"


def test_valid_payload_with_form_data():
    """Test payload with extra form data fields."""
    payload = WorkflowLaunchPayload(
        launch={"workflow": "de-novo-design", "tool": "bindcraft"},
        formData={
            "workflow": "de-novo-design",
            "tool": "bindcraft",
            "sample": "test",
            "input": "/path/to/file",
            "param": 42,
        },
        s3InputKey="inputs/samplesheets/test.csv",
    )

    assert payload.formData.tool == "bindcraft"
    assert payload.formData.model_extra["sample"] == "test"


def test_payload_extra_fields_forbidden():
    """Test that extra fields are not allowed in payload."""
    with pytest.raises(ValidationError):
        WorkflowLaunchPayload(
            launch={"workflow": "de-novo-design", "tool": "bindcraft"},
            formData={"workflow": "de-novo-design", "tool": "bindcraft"},
            s3InputKey="inputs/samplesheets/test.csv",
            unknownField="value",
        )


def test_payload_requires_s3_input_key():
    with pytest.raises(ValidationError):
        WorkflowLaunchPayload(
            launch={"workflow": "de-novo-design", "tool": "bindcraft"},
            formData={"workflow": "de-novo-design", "tool": "bindcraft"},
        )


def test_valid_response():
    """Test creating a valid launch response."""
    response = WorkflowLaunchResponse(
        message="Workflow launched",
        runId="run_123",
        status="submitted",
        submitTime=datetime(2024, 1, 1, 12, 0, 0),
    )

    assert response.message == "Workflow launched"
    assert response.runId == "run_123"
    assert response.status == "submitted"
    assert response.submitTime.year == 2024


def test_valid_cancel_response():
    """Test creating a valid cancel response."""
    response = CancelWorkflowResponse(
        message="Cancelled",
        runId="run_123",
        status="cancelled",
    )

    assert response.message == "Cancelled"
    assert response.runId == "run_123"
    assert response.status == "cancelled"


def test_valid_run_info():
    """Test creating valid run info."""
    run_info = RunInfo(
        id="run_123",
        run="test-run",
        workflow="test-workflow",
        status="running",
        date="2024-01-01",
        cancel="false",
    )

    assert run_info.id == "run_123"
    assert run_info.status == "running"


def test_empty_runs_list():
    """Test response with empty runs list."""
    response = ListRunsResponse(
        runs=[],
        total=0,
        limit=50,
        offset=0,
    )

    assert response.runs == []
    assert response.total == 0


def test_runs_list_with_data():
    """Test response with run data."""
    run_info = RunInfo(
        id="run_123",
        run="test",
        workflow="wf",
        status="done",
        date="2024-01-01",
        cancel="false",
    )
    response = ListRunsResponse(
        runs=[run_info],
        total=1,
        limit=50,
        offset=0,
    )

    assert len(response.runs) == 1
    assert response.total == 1


def test_valid_logs():
    """Test creating valid launch logs."""
    logs = LaunchLogs(
        truncated=False,
        entries=["log line 1", "log line 2"],
        rewindToken="token1",
        forwardToken="token2",
        pending=False,
        message="Logs retrieved",
    )

    assert len(logs.entries) == 2
    assert logs.truncated is False


def test_valid_details():
    """Test creating valid launch details."""
    details = LaunchDetails(
        requiresAttention=False,
        status="completed",
        ownerId=123,
        repository="https://github.com/test/repo",
        id="launch_123",
        submit="2024-01-01T12:00:00",
        start="2024-01-01T12:01:00",
        complete="2024-01-01T12:10:00",
        dateCreated="2024-01-01T12:00:00",
        lastUpdated="2024-01-01T12:10:00",
        runName="test-run",
        sessionId="session_123",
        profile="standard",
        workDir="/work",
        commitId="abc123",
        userName="testuser",
        scriptId="script_123",
        revision="main",
        commandLine="nextflow run",
        projectName="test-project",
        scriptName="main.nf",
        launchId="launch_123",
        configFiles=["nextflow.config"],
        params={"test": "value"},
    )

    assert details.status == "completed"
    assert details.ownerId == 123


def test_dataset_upload_request_valid():
    """Test creating valid DatasetUploadRequest."""
    request = DatasetUploadRequest(
        formData={"sample": "test", "input": "/path/file"},
    )

    assert request.formData == {"sample": "test", "input": "/path/file"}


def test_dataset_upload_request_empty_form_data():
    """Test DatasetUploadRequest validator rejects empty formData."""
    with pytest.raises(ValidationError, match="formData cannot be empty"):
        DatasetUploadRequest(
            formData={},
        )


# Tests for Job Listing schemas


def test_status_mapping_submitted():
    """Test pipeline status SUBMITTED maps to 'In queue'."""
    assert map_pipeline_status_to_ui("SUBMITTED") == "In queue"
    assert map_pipeline_status_to_ui(PipelineStatus.SUBMITTED.value) == UIStatus.IN_QUEUE.value


def test_status_mapping_running():
    """Test pipeline status RUNNING maps to 'In progress'."""
    assert map_pipeline_status_to_ui("RUNNING") == "In progress"
    assert map_pipeline_status_to_ui(PipelineStatus.RUNNING.value) == UIStatus.IN_PROGRESS.value


def test_status_mapping_succeeded():
    """Test pipeline status SUCCEEDED maps to 'Completed'."""
    assert map_pipeline_status_to_ui("SUCCEEDED") == "Completed"
    assert map_pipeline_status_to_ui(PipelineStatus.SUCCEEDED.value) == UIStatus.COMPLETED.value


def test_status_mapping_failed():
    """Test pipeline status FAILED maps to 'Failed'."""
    assert map_pipeline_status_to_ui("FAILED") == "Failed"
    assert map_pipeline_status_to_ui(PipelineStatus.FAILED.value) == UIStatus.FAILED.value


def test_status_mapping_unknown():
    """Test pipeline status UNKNOWN maps to 'Failed'."""
    assert map_pipeline_status_to_ui("UNKNOWN") == "Failed"
    assert map_pipeline_status_to_ui(PipelineStatus.UNKNOWN.value) == UIStatus.FAILED.value


def test_status_mapping_cancelled():
    """Test pipeline status CANCELLED maps to 'Stopped'."""
    assert map_pipeline_status_to_ui("CANCELLED") == "Stopped"
    assert map_pipeline_status_to_ui(PipelineStatus.CANCELLED.value) == UIStatus.STOPPED.value


def test_status_mapping_invalid():
    """Test invalid pipeline status defaults to 'Failed'."""
    assert map_pipeline_status_to_ui("INVALID_STATUS") == "Failed"
    assert map_pipeline_status_to_ui("") == "Failed"


def test_job_list_item_valid():
    """Test creating valid JobListItem."""
    job = JobListItem(
        id="wf-123",
        jobName="Test Job",
        workflow="BindCraft",
        tool="BindCraft",
        status="Completed",
        submittedAt=datetime(2026, 2, 1, 10, 0, 0),
        score=0.95,
    )

    assert job.id == "wf-123"
    assert job.jobName == "Test Job"
    assert job.workflow == "BindCraft"
    assert job.status == "Completed"
    assert job.score == 0.95


def test_job_list_item_optional_fields():
    """Test JobListItem with optional fields as None."""
    job = JobListItem(
        id="wf-456",
        jobName="Another Job",
        workflow="Unknown",
        tool="Unknown",
        status="In progress",
        submittedAt=datetime(2026, 2, 2, 11, 0, 0),
        score=None,
    )

    assert job.workflow == "Unknown"
    assert job.score is None


def test_job_list_response_valid():
    """Test creating valid JobListResponse."""
    jobs = [
        JobListItem(
            id="wf-1",
            jobName="Job 1",
            workflow="Unknown",
            tool="Unknown",
            status="Completed",
            submittedAt=datetime(2026, 2, 1, 10, 0, 0),
        ),
        JobListItem(
            id="wf-2",
            jobName="Job 2",
            workflow="Unknown",
            tool="Unknown",
            status="In progress",
            submittedAt=datetime(2026, 2, 2, 11, 0, 0),
        ),
    ]

    response = JobListResponse(
        jobs=jobs,
        total=100,
        limit=10,
        offset=0,
    )

    assert len(response.jobs) == 2
    assert response.total == 100
    assert response.limit == 10
    assert response.offset == 0


def test_job_list_response_empty():
    """Test JobListResponse with empty job list."""
    response = JobListResponse(
        jobs=[],
        total=0,
        limit=10,
        offset=0,
    )

    assert len(response.jobs) == 0
    assert response.total == 0


# ============================================================================
# Tests for WispsSequenceItem and WispsDatasetUploadRequest
# ============================================================================


def test_sequence_item_query():
    item = WispsSequenceItem(id="seq-1", group="query")
    assert item.id == "seq-1"
    assert item.group == "query"


def test_sequence_item_target():
    item = WispsSequenceItem(id="seq-2", group="target")
    assert item.group == "target"


def test_sequence_item_invalid_group():
    with pytest.raises(ValidationError):
        WispsSequenceItem(id="seq-3", group="invalid")


def test_sequence_item_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        WispsSequenceItem(id="seq-4", group="query", extra="nope")


def test_interaction_screening_request_valid():
    req = WispsDatasetUploadRequest(
        sequences=[
            {"id": "q1", "group": "query"},
            {"id": "t1", "group": "target"},
        ],
        runId="run-xyz",
    )
    assert len(req.sequences) == 2
    assert req.runId == "run-xyz"
    assert req.sequences[0].group == "query"
    assert req.sequences[1].group == "target"


def test_interaction_screening_request_empty_sequences():
    # Empty sequences list is accepted by the schema; enforcement is in the service layer
    req = WispsDatasetUploadRequest(sequences=[], runId="run-1")
    assert req.sequences == []


def test_interaction_screening_request_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        WispsDatasetUploadRequest(
            sequences=[{"id": "q1", "group": "query"}],
            runId="run-1",
            extra="bad",
        )


def test_get_encoded_account_details_encodes_email_and_ip():
    details = WorkflowUserDetails(user_email="user@example.com", ip_address="1.2.3.4")
    assert details.get_encoded_account_details() == "dXNlckBleGFtcGxlLmNvbQ==:MS4yLjMuNA=="


def test_get_encoded_account_details_without_ip_encodes_email_only():
    details = WorkflowUserDetails(user_email="user@example.com", ip_address="")
    assert details.get_encoded_account_details() == "dXNlckBleGFtcGxlLmNvbQ=="


# =============================================================================
# Single-prediction entity validation
# =============================================================================


def _protein(sequence="ACDEFGHIK", copy_number=1):
    return SinglePredictionEntity(
        id="seq1", moleculeType="protein", copyNumber=copy_number, sequence=sequence
    )


def test_size_limit_defaults_and_boltz_potentials():
    assert single_prediction_size_limit("colabfold", False) == 4000
    assert single_prediction_size_limit("boltz", False) == 4000
    assert single_prediction_size_limit("alphafold2", False) == 2000
    assert single_prediction_size_limit("boltz", True) == 2000
    assert single_prediction_size_limit("colabfold", True) == 4000


def test_validate_single_prediction_accepts_valid_input():
    validate_single_prediction_entities([_protein()], "colabfold")


def test_validate_single_prediction_requires_entities():
    with pytest.raises(ValueError, match="At least one entity"):
        validate_single_prediction_entities([], "colabfold")


def test_validate_single_prediction_counts_copies_toward_limit():
    validate_single_prediction_entities([_protein(copy_number=52)], "colabfold")
    with pytest.raises(ValueError, match="Too many entities"):
        validate_single_prediction_entities([_protein(copy_number=53)], "colabfold")


def test_validate_single_prediction_requires_protein():
    dna = SinglePredictionEntity(id="d", moleculeType="dna", copyNumber=1, sequence="ACGT")
    with pytest.raises(ValueError, match="must be a protein"):
        validate_single_prediction_entities([dna], "boltz")


def test_single_prediction_entity_rejects_copy_number_below_one():
    with pytest.raises(ValidationError):
        SinglePredictionEntity(id="p", moleculeType="protein", copyNumber=0, sequence="ACD")


def test_single_prediction_entity_rejects_unknown_molecule_type():
    with pytest.raises(ValidationError):
        SinglePredictionEntity(id="p", moleculeType="peptide", copyNumber=1, sequence="ACD")


def test_single_prediction_entity_rejects_empty_id():
    with pytest.raises(ValidationError):
        SinglePredictionEntity(id="", moleculeType="protein", copyNumber=1, sequence="ACD")


def test_validate_single_prediction_ligand_uses_fixed_size():
    protein = _protein(sequence="A" * 3960)
    ligand = SinglePredictionEntity(
        id="lig", moleculeType="ligand", copyNumber=1, sequence="CC(=O)O"
    )
    # 3960 + 30 (ligand) = 3990 < 4000
    validate_single_prediction_entities([protein, ligand], "colabfold")
    # Two ligand copies: 3960 + 60 = 4020 >= 4000
    ligand_two = SinglePredictionEntity(
        id="lig", moleculeType="ligand", copyNumber=2, sequence="CC(=O)O"
    )
    with pytest.raises(ValueError, match="less than 4000"):
        validate_single_prediction_entities([protein, ligand_two], "colabfold")


def test_validate_single_prediction_size_limit_is_exclusive():
    with pytest.raises(ValueError, match="less than 2000"):
        validate_single_prediction_entities([_protein(sequence="A" * 2000)], "alphafold2")
    validate_single_prediction_entities([_protein(sequence="A" * 1999)], "alphafold2")


# Short lists of valid/invalid examples per molecule type. Parametrized below
# so adding a newly-discovered edge case is a one-line change.
VALID_PROTEIN_SEQUENCES = ["ACDEFGHIK", "acdefghik", "ACD\nEFG hik"]
INVALID_PROTEIN_SEQUENCES = ["", "ACDEFXZ", "XYZ123"]

VALID_DNA_SEQUENCES = ["ACGT", "acgt", "AT GC"]
INVALID_DNA_SEQUENCES = ["", "ACGU", "ACGX"]

VALID_RNA_SEQUENCES = ["ACGU", "acgu"]
INVALID_RNA_SEQUENCES = ["", "ACGT", "ACGX"]

VALID_LIGAND_SMILES = ["CC(=O)O", "c1ccccc1"]
INVALID_LIGAND_SMILES = ["", "not-a-smiles("]

VALID_CCD_CODES = ["ATP", "atp", "ADP"]
INVALID_CCD_CODES = ["XYZ", "TOOLONGCODE"]


@pytest.mark.parametrize("sequence", VALID_PROTEIN_SEQUENCES)
def test_validate_single_prediction_accepts_valid_protein_sequences(sequence):
    validate_single_prediction_entities([_protein(sequence=sequence)], "colabfold")


@pytest.mark.parametrize("sequence", INVALID_PROTEIN_SEQUENCES)
def test_validate_single_prediction_rejects_invalid_protein_sequences(sequence):
    protein = SinglePredictionEntity(
        id="p", moleculeType="protein", copyNumber=1, sequence=sequence
    )
    with pytest.raises(ValueError, match="canonical"):
        validate_single_prediction_entities([protein], "colabfold")


@pytest.mark.parametrize("sequence", VALID_DNA_SEQUENCES)
def test_validate_single_prediction_accepts_valid_dna_sequences(sequence):
    dna = SinglePredictionEntity(id="d", moleculeType="dna", copyNumber=1, sequence=sequence)
    validate_single_prediction_entities([_protein(), dna], "colabfold")


@pytest.mark.parametrize("sequence", INVALID_DNA_SEQUENCES)
def test_validate_single_prediction_rejects_invalid_dna_sequences(sequence):
    dna = SinglePredictionEntity(id="d", moleculeType="dna", copyNumber=1, sequence=sequence)
    with pytest.raises(ValueError, match="DNA characters"):
        validate_single_prediction_entities([_protein(), dna], "colabfold")


@pytest.mark.parametrize("sequence", VALID_RNA_SEQUENCES)
def test_validate_single_prediction_accepts_valid_rna_sequences(sequence):
    rna = SinglePredictionEntity(id="r", moleculeType="rna", copyNumber=1, sequence=sequence)
    validate_single_prediction_entities([_protein(), rna], "colabfold")


@pytest.mark.parametrize("sequence", INVALID_RNA_SEQUENCES)
def test_validate_single_prediction_rejects_invalid_rna_sequences(sequence):
    rna = SinglePredictionEntity(id="r", moleculeType="rna", copyNumber=1, sequence=sequence)
    with pytest.raises(ValueError, match="RNA characters"):
        validate_single_prediction_entities([_protein(), rna], "colabfold")


@pytest.mark.parametrize("sequence", VALID_LIGAND_SMILES)
def test_validate_single_prediction_accepts_valid_ligand_smiles(sequence):
    ligand = SinglePredictionEntity(
        id="lig", moleculeType="ligand", copyNumber=1, sequence=sequence
    )
    validate_single_prediction_entities([_protein(), ligand], "colabfold")


@pytest.mark.parametrize("sequence", INVALID_LIGAND_SMILES)
def test_validate_single_prediction_rejects_invalid_ligand_smiles(sequence):
    ligand = SinglePredictionEntity(
        id="lig", moleculeType="ligand", copyNumber=1, sequence=sequence
    )
    with pytest.raises(ValueError, match="valid SMILES"):
        validate_single_prediction_entities([_protein(), ligand], "colabfold")


@pytest.mark.parametrize("code", VALID_CCD_CODES)
def test_validate_single_prediction_accepts_valid_ccd_codes(code):
    ccd = SinglePredictionEntity(id="c", moleculeType="ccd", copyNumber=1, sequence=code)
    validate_single_prediction_entities([_protein(), ccd], "colabfold")


@pytest.mark.parametrize("code", INVALID_CCD_CODES)
def test_validate_single_prediction_rejects_invalid_ccd_codes(code):
    ccd = SinglePredictionEntity(id="c", moleculeType="ccd", copyNumber=1, sequence=code)
    with pytest.raises(ValueError, match="unsupported ligand code"):
        validate_single_prediction_entities([_protein(), ccd], "colabfold")
