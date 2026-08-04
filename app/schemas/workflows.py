"""Pydantic models shared across workflow endpoints."""

from __future__ import annotations

import base64
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from rdkit import Chem, RDLogger

# RDKit logs a warning/error to stderr for every unparseable SMILES by default;
# invalid SMILES are an expected user-input case here, not something to log.
RDLogger.DisableLog("rdApp.*")

WorkflowName = Literal[
    "single-prediction", "de-novo-design", "bulk-prediction", "interaction-screening"
]
WorkflowTool = Literal["alphafold2", "bindcraft", "boltz", "colabfold", "rfdiffusion"]
MoleculeType = Literal["protein", "rna", "dna", "ligand", "ccd"]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SINGLE_PREDICTION_MAX_ENTITIES = 52
SINGLE_PREDICTION_LIGAND_SIZE = 30
SINGLE_PREDICTION_SIZE_LIMITS: dict[str, int] = {
    "alphafold2": 2000,
    "colabfold": 4000,
    "boltz": 4000,
}
SINGLE_PREDICTION_BOLTZ_POTENTIALS_SIZE_LIMIT = 2000


class PipelineStatus(StrEnum):
    """Pipeline status values from Seqera Platform."""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class UIStatus(StrEnum):
    """User-facing status values for the frontend."""

    IN_QUEUE = "In queue"
    IN_PROGRESS = "In progress"
    COMPLETED = "Completed"
    FAILED = "Failed"
    STOPPED = "Stopped"


def map_pipeline_status_to_ui(pipeline_status: str) -> str:
    """Map Seqera pipeline status to UI-friendly status."""
    status_mapping = {
        PipelineStatus.SUBMITTED.value: UIStatus.IN_QUEUE.value,
        PipelineStatus.RUNNING.value: UIStatus.IN_PROGRESS.value,
        PipelineStatus.SUCCEEDED.value: UIStatus.COMPLETED.value,
        PipelineStatus.FAILED.value: UIStatus.FAILED.value,
        PipelineStatus.UNKNOWN.value: UIStatus.FAILED.value,
        PipelineStatus.CANCELLED.value: UIStatus.STOPPED.value,
    }
    return status_mapping.get(pipeline_status, UIStatus.FAILED.value)


class WorkflowLaunchForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowName = Field(..., description="Workflow name")
    tool: WorkflowTool = Field(..., description="Requested tool name")
    configProfiles: list[str] = Field(
        default_factory=list, description="Profiles that customize the workflow"
    )
    runName: str | None = Field(default=None, description="Human-readable workflow run name")
    paramsText: str | None = Field(default=None, description="YAML-style parameter overrides")

    @field_validator("tool", "workflow")
    @classmethod
    def validate_tool(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("This field is required")
        return stripped


class WorkflowFormData(BaseModel):
    """
    Model for form data submitted by the frontend - this will
    be different for each model, and may include additional fields.
    """

    # Allow extra fields to be included in the form data
    model_config = ConfigDict(extra="allow")

    @property
    def extra_fields(self) -> dict[str, Any]:
        return self.model_extra or {}

    workflow: WorkflowName = Field(..., description="Workflow name")
    tool: WorkflowTool = Field(..., description="Requested tool name")
    configProfiles: list[str] = Field(
        default_factory=list, description="Profiles that customize the workflow"
    )
    runName: str | None = Field(default=None, description="Human-readable workflow run name")
    paramsText: str | None = Field(default=None, description="YAML-style parameter overrides")
    sample_id: str | None = Field(default=None, description="Sample ID for the workflow run")


class WorkflowUserDetails(BaseModel):
    """
    Details recorded in workflow runs - required by compute providers
    """

    user_email: str = Field(..., description="Email address of the user")
    ip_address: str = Field(..., description="IP address of the user")

    def get_encoded_account_details(self) -> str:
        """Return the `-A` account string for cluster job submission.

        Encodes email and IP as base64 (colon-joined) so the account string
        never leaks the user's raw email/IP into cluster logs or job metadata.
        """
        encoded_email = base64.b64encode(self.user_email.encode()).decode()
        if not self.ip_address:
            return encoded_email
        encoded_ip = base64.b64encode(self.ip_address.encode()).decode()
        return f"{encoded_email}:{encoded_ip}"


class WispsFormData(WorkflowFormData):
    """Form data for WISPS workflows (interaction-screening, bulk-prediction)."""

    fastaS3Uri: str = Field(
        ..., description="S3 URI of the combined FASTA file to split and screen"
    )
    splitOutputDir: str = Field(
        ..., description="Cluster filesystem path for per-sequence FASTA files"
    )


class SinglePredictionEntity(BaseModel):
    """A single entity row submitted for a single-prediction run."""

    model_config = ConfigDict(extra="allow")

    id: NonEmptyStr | None = None
    moleculeType: MoleculeType
    copyNumber: PositiveInt = 1
    sequence: Annotated[str, StringConstraints(strip_whitespace=True)] = ""


def _is_valid_smiles(value: str) -> bool:
    """Check whether a string is a chemically valid SMILES, using RDKit."""
    return bool(value) and Chem.MolFromSmiles(value) is not None


def _entity_prediction_size(entity: SinglePredictionEntity) -> int:
    """Prediction size for one copy of an entity (ligands use a fixed size)."""
    if entity.moleculeType in ("ligand", "ccd"):
        return SINGLE_PREDICTION_LIGAND_SIZE
    return len(re.sub(r"\s+", "", entity.sequence or ""))


def single_prediction_size_limit(tool: str, boltz_use_potentials: bool) -> int:
    """Exclusive upper bound on the total prediction size for the given tool."""
    if tool == "boltz" and boltz_use_potentials:
        return SINGLE_PREDICTION_BOLTZ_POTENTIALS_SIZE_LIMIT
    return SINGLE_PREDICTION_SIZE_LIMITS.get(tool, SINGLE_PREDICTION_SIZE_LIMITS["colabfold"])


def validate_single_prediction_entities(
    entities: list[SinglePredictionEntity],
    tool: str,
    *,
    boltz_use_potentials: bool = False,
) -> None:
    """Validate entity count, protein presence, and total prediction size.

    Raises ``ValueError`` with a user-facing message on the first violation.
    """
    if not entities:
        raise ValueError("At least one entity is required.")

    total_copies = 0
    total_size = 0
    has_protein = False
    for entity in entities:
        total_copies += entity.copyNumber
        if entity.moleculeType == "protein":
            has_protein = True
        if entity.moleculeType == "ligand" and not _is_valid_smiles(entity.sequence):
            label = entity.id or "unnamed entity"
            raise ValueError(
                f"Ligand entity '{label}' has an invalid SMILES string: '{entity.sequence}'"
            )
        total_size += _entity_prediction_size(entity) * entity.copyNumber

    if total_copies > SINGLE_PREDICTION_MAX_ENTITIES:
        raise ValueError(
            f"Too many entities: {total_copies} including copies. "
            f"The maximum allowed is {SINGLE_PREDICTION_MAX_ENTITIES}."
        )

    if not has_protein:
        raise ValueError(
            "At least one entity must be a protein. "
            "This workflow cannot run without a protein input."
        )

    limit = single_prediction_size_limit(tool, boltz_use_potentials)
    if total_size >= limit:
        raise ValueError(
            f"Total prediction size ({total_size}) must be less than {limit} for {tool}."
        )


MAX_HOTSPOT_RESIDUES = 8
MIN_DESIGN_LENGTH = 65
MAX_DESIGN_LENGTH = 150


class ProteinDjFormData(WorkflowFormData):
    """Form data for the ProteinDJ (rfdiffusion) de-novo-design workflow."""

    starting_pdb: str = Field(..., description="S3 URI of the uploaded starting PDB file")
    target_hotspot_residues: str = Field(
        ..., description="Comma-separated hotspot residues, e.g. 'A20,A21'"
    )
    number_of_final_designs: int = Field(..., ge=1, description="Number of designs to generate")
    min_length: int = Field(..., ge=MIN_DESIGN_LENGTH, description="Minimum binder length")
    max_length: int = Field(..., le=MAX_DESIGN_LENGTH, description="Maximum binder length")

    @field_validator("starting_pdb")
    @classmethod
    def _validate_starting_pdb(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("This field is required")
        return stripped

    @field_validator("target_hotspot_residues")
    @classmethod
    def _validate_hotspot_residues(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("This field is required")
        residue_count = len([token for token in stripped.split(",") if token.strip()])
        if residue_count > MAX_HOTSPOT_RESIDUES:
            raise ValueError(
                f"Too many hotspot residues ({residue_count}); only up to "
                f"{MAX_HOTSPOT_RESIDUES} are supported"
            )
        return stripped

    @model_validator(mode="after")
    def _validate_length_range(self) -> ProteinDjFormData:
        if self.min_length > self.max_length:
            raise ValueError("min_length must not exceed max_length")
        return self


class WorkflowLaunchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch: WorkflowLaunchForm
    s3InputKey: str = Field(
        ...,
        description="S3 object key for the workflow input CSV samplesheet",
    )
    formData: WorkflowFormData = Field(
        ...,
        description="Form data submitted for the workflow run",
    )


class WorkflowLaunchResponse(BaseModel):
    message: str
    runId: str
    status: str
    submitTime: datetime


class CancelWorkflowResponse(BaseModel):
    message: str
    runId: str
    status: str


class RunInfo(BaseModel):
    id: str
    run: str
    workflow: str
    status: str
    date: str
    cancel: str


class ListRunsResponse(BaseModel):
    runs: list[RunInfo]
    total: int
    limit: int
    offset: int


class LaunchLogs(BaseModel):
    truncated: bool
    entries: list[str]
    rewindToken: str
    forwardToken: str
    pending: bool
    message: str
    downloads: list[dict[str, str]] = Field(default_factory=list)


class LaunchDetails(BaseModel):
    requiresAttention: bool
    status: str
    ownerId: int
    repository: str
    id: str
    submit: str
    start: str
    complete: str
    dateCreated: str
    lastUpdated: str
    runName: str
    sessionId: str
    profile: str
    workDir: str
    commitId: str
    userName: str
    scriptId: str
    revision: str
    commandLine: str
    projectName: str
    scriptName: str
    launchId: str
    configFiles: list[str]
    params: dict[str, str]


class DatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formData: dict[str, Any]

    @field_validator("formData")
    @classmethod
    def validate_form_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("formData cannot be empty")
        return value


class DatasetUploadResponse(BaseModel):
    message: str
    datasetId: str
    success: bool
    splitOutputDir: str | None = None
    details: dict[str, Any] | None = None


class InteractionScreeningDatasetUploadResponse(DatasetUploadResponse):
    """Dataset upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str


class S3DatasetUploadResponse(BaseModel):
    message: str
    s3Key: str
    s3Uri: str
    success: bool
    splitOutputDir: str | None = None


class InteractionScreeningS3UploadResponse(S3DatasetUploadResponse):
    """S3 upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str


class RunInputPresignedUrlResponse(BaseModel):
    runId: str
    s3Key: str
    presignedUrl: str


class WispsSequenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    sequence: str | None = None
    group: Literal["query", "target"] | None = None


class WispsDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequences: list[WispsSequenceItem]
    runId: str


class PdbUploadResponse(BaseModel):
    """Response model for PDB file upload."""

    message: str
    success: bool
    fileId: str = Field(..., description="S3 file key/identifier")
    fileName: str = Field(..., description="Original filename")
    s3Uri: str = Field(..., description="Full S3 URI (s3://bucket/key) for dataset creation")
    details: dict[str, Any] | None = Field(default=None, description="Additional upload details")


class FastaUploadResponse(BaseModel):
    """Response model for FASTA file upload."""

    message: str
    success: bool
    fileId: str = Field(..., description="S3 file key/identifier")
    fileName: str = Field(..., description="Original filename")
    s3Uri: str = Field(..., description="Full S3 URI (s3://bucket/key)")
    presignedUrl: str = Field(..., description="Pre-signed HTTPS URL for the FASTA file")
    details: dict[str, Any] | None = Field(default=None, description="Additional upload details")


class JobListItem(BaseModel):
    """Individual job item in the job listing."""

    id: str = Field(..., description="Workflow run ID")
    seqeraRunId: str | None = Field(None, description="Seqera run ID")
    jobName: str = Field(..., description="Human-readable job name")
    workflow: str = Field(..., description="Workflow name from the workflows table")
    tool: str = Field(..., description="Tool used (e.g., BindCraft)")
    status: str = Field(..., description="UI-friendly status (e.g., Completed, In progress)")
    submittedAt: datetime = Field(..., description="Submission date and time")
    score: float | None = Field(None, description="Job score/metric")
    finalDesignCount: int | None = Field(None, description="Number of final designs")


class JobListResponse(BaseModel):
    """Paginated response for job listing."""

    jobs: list[JobListItem] = Field(default_factory=list, description="List of jobs")
    total: int = Field(..., description="Total number of jobs matching the criteria")
    limit: int = Field(..., description="Maximum number of items per page")
    offset: int = Field(..., description="Number of items skipped")
    seqeraUnavailable: bool = Field(
        False,
        description="True when Seqera could not be reached; job status and logs will be unavailable",
    )


class JobDetailsResponse(BaseModel):
    """Detailed response for a single job."""

    id: str = Field(..., description="Workflow run ID")
    jobName: str = Field(..., description="Human-readable job name")
    workflow: str = Field(..., description="Workflow name from the workflows table")
    tool: str = Field(..., description="Tool used (e.g., BindCraft); 'Unknown' if not recorded")
    status: str = Field(..., description="UI-friendly status")
    submittedAt: datetime = Field(..., description="Submission date and time")
    score: float | None = Field(None, description="Job score/metric")
    finalDesignCount: int | None = Field(None, description="Number of final designs")


class JobSettingParamsResponse(BaseModel):
    """Submitted form settings for a job result view."""

    runId: str
    settingParams: dict[str, Any] | None = None


class ResultLogEntry(BaseModel):
    """Frontend-friendly representation of a single log line."""

    index: int
    raw: str
    message: str
    level: str = "INFO"
    timestamp: str | None = None


class ResultLogsResponse(BaseModel):
    """Workflow logs for a result view."""

    runId: str
    truncated: bool
    pending: bool
    message: str
    rewindToken: str
    forwardToken: str
    downloads: list[dict[str, str]] = Field(default_factory=list)
    entries: list[str] = Field(default_factory=list)
    formattedEntries: list[ResultLogEntry] = Field(default_factory=list)


class ResultDownloadItem(BaseModel):
    """Single pre-signed download link for a result artifact."""

    label: str
    key: str
    url: str
    category: str


class ResultDownloadsResponse(BaseModel):
    """Download links for result artifacts."""

    runId: str
    downloads: list[ResultDownloadItem] = Field(default_factory=list)


class ResultSnapshotsResponse(BaseModel):
    """Snapshot download links for a result view."""

    runId: str
    snapshots: list[ResultDownloadItem] = Field(default_factory=list)


class ResultReportResponse(BaseModel):
    """Primary HTML report link for a result view."""

    runId: str
    report: ResultDownloadItem | None = None


class DeleteJobResponse(BaseModel):
    """Response for single job deletion."""

    runId: str
    deleted: bool
    cancelledBeforeDelete: bool = False
    message: str


class BulkDeleteJobsRequest(BaseModel):
    """Request payload for bulk job deletion."""

    runIds: list[str] = Field(..., min_length=1)


class BulkDeleteJobsResponse(BaseModel):
    """Response for bulk job deletion."""

    deleted: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)


TERMINAL_SEQERA_STATUSES = frozenset(
    {
        PipelineStatus.SUCCEEDED.value,
        PipelineStatus.FAILED.value,
        PipelineStatus.CANCELLED.value,
    }
)
