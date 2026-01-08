"""Pydantic models shared across workflow endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkflowLaunchForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline: str = Field(..., description="Workflow pipeline repository or URL")
    revision: str | None = Field(
        default=None, description="Revision or branch of the pipeline to run"
    )
    configProfiles: list[str] = Field(
        default_factory=list, description="Profiles that customize the workflow"
    )
    runName: str | None = Field(default=None, description="Human-readable workflow run name")
    paramsText: str | None = Field(default=None, description="YAML-style parameter overrides")

    @field_validator("pipeline")
    @classmethod
    def validate_pipeline(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("pipeline is required")
        return value.strip()


class WorkflowLaunchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch: WorkflowLaunchForm
    datasetId: str | None = Field(
        default=None,
        description="Optional Seqera dataset ID to attach to the workflow",
    )
    formData: dict[str, Any] | None = Field(
        default=None,
        description="Optional form data to convert to CSV and upload as a dataset",
    )
    pdbFileKey: str | None = Field(
        default=None,
        description="Optional S3 file key for PDB file. A pre-signed URL will be generated and added to formData",
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
    datasetName: str | None = Field(default=None)
    datasetDescription: str | None = Field(default=None)

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


class PdbUploadResponse(BaseModel):
    """Response model for PDB file upload."""

    message: str
    success: bool
    fileId: str = Field(..., description="S3 file key/identifier")
    fileName: str = Field(..., description="Original filename")
    fileUrl: str = Field(..., description="S3 file location")
    details: dict[str, Any] | None = Field(
        default=None, description="Additional upload details"
    )
