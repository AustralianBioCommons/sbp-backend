"""Pydantic models shared across workflow endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkflowLaunchForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline: str = Field(..., description="Workflow pipeline repository or URL")
    revision: Optional[str] = Field(
        default=None, description="Revision or branch of the pipeline to run"
    )
    configProfiles: List[str] = Field(
        default_factory=list, description="Profiles that customize the workflow"
    )
    runName: Optional[str] = Field(
        default=None, description="Human-readable workflow run name"
    )
    paramsText: Optional[str] = Field(
        default=None, description="YAML-style parameter overrides"
    )

    @field_validator("pipeline")
    @classmethod
    def validate_pipeline(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("pipeline is required")
        return value.strip()


class WorkflowLaunchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch: WorkflowLaunchForm
    datasetId: Optional[str] = Field(
        default=None,
        description="Optional Seqera dataset ID to attach to the workflow",
    )
    formData: Optional[Dict[str, Any]] = Field(
        default=None,
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
    runs: List[RunInfo]
    total: int
    limit: int
    offset: int


class LaunchLogs(BaseModel):
    truncated: bool
    entries: List[str]
    rewindToken: str
    forwardToken: str
    pending: bool
    message: str
    downloads: List[Dict[str, str]] = Field(default_factory=list)


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
    configFiles: List[str]
    params: Dict[str, str]


class DatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formData: Dict[str, Any]
    datasetName: Optional[str] = Field(default=None)
    datasetDescription: Optional[str] = Field(default=None)

    @field_validator("formData")
    @classmethod
    def validate_form_data(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not value:
            raise ValueError("formData cannot be empty")
        return value


class DatasetUploadResponse(BaseModel):
    message: str
    datasetId: str
    success: bool
    details: Optional[Dict[str, Any]] = None
