"""Pydantic models shared across workflow endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkflowName = Literal[
    "single-prediction", "de-novo-design", "bulk-prediction", "interaction-screening"
]
WorkflowTool = Literal["alphafold2", "bindcraft", "boltz", "boltzgen", "colabfold", "rfdiffusion"]


class PipelineStatus(str, Enum):
    """Pipeline status values from Seqera Platform."""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class UIStatus(str, Enum):
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


class WorkflowLaunchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch: WorkflowLaunchForm
    datasetId: str = Field(
        ...,
        description="Seqera dataset ID to attach to the workflow",
    )
    formData: WorkflowFormData = Field(
        ...,
        description="Optional form data to convert to CSV and upload as a dataset",
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
    details: dict[str, Any] | None = None


class SequenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: Literal["query", "target"]


class InteractionScreeningDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequences: list[SequenceItem]
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
