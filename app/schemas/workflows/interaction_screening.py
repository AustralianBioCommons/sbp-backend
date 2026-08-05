"""Schemas for the interaction-screening workflow.

``WispsFormData``/``WispsSequenceItem``/``WispsDatasetUploadRequest`` back the
shared WISPS pipeline used by both interaction-screening and bulk-prediction
(see ``bulk_prediction.py``); the ``InteractionScreening*`` response models
below are specific to this workflow, which always returns a split output dir.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .shared import DatasetUploadResponse, S3DatasetUploadResponse, WorkflowFormData


class WispsFormData(WorkflowFormData):
    """Form data for WISPS workflows (interaction-screening, bulk-prediction)."""

    fastaS3Uri: str = Field(
        ..., description="S3 URI of the combined FASTA file to split and screen"
    )
    splitOutputDir: str = Field(
        ..., description="Cluster filesystem path for per-sequence FASTA files"
    )


class WispsSequenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    sequence: str | None = None
    group: Literal["query", "target"] | None = None


class WispsDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequences: list[WispsSequenceItem]
    runId: str


class InteractionScreeningDatasetUploadResponse(DatasetUploadResponse):
    """Dataset upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str


class InteractionScreeningS3UploadResponse(S3DatasetUploadResponse):
    """S3 upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str
