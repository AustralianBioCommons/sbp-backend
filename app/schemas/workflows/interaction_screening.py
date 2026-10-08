"""Schemas for the interaction-screening workflow.

``WispsFormData``/``WispsSequenceItem``/``WispsDatasetUploadRequest`` back
bulk-prediction's split-FASTA flow (see ``bulk_prediction.py``). Interaction
screening instead uploads one query and one target FASTA, and its samplesheet
has exactly two rows (g1=query, g2=target) pointing at those files.
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

    # Used as the per-sequence split filename (`<id>.fasta`) that Nextflow reads,
    # so characters Nextflow treats as glob syntax (e.g. "[", "]") must be excluded.
    id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    sequence: str | None = None
    group: Literal["query", "target"] | None = None


class WispsDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequences: list[WispsSequenceItem]
    runId: str


class InteractionScreeningFormData(WorkflowFormData):
    """Form data for interaction-screening: one FASTA per group, each staged to
    Gadi as-is and referenced by its own samplesheet row (no prerun split)."""

    queryFastaS3Uri: str = Field(..., description="S3 URI of the query multi-FASTA file")
    targetFastaS3Uri: str = Field(..., description="S3 URI of the target multi-FASTA file")


class InteractionScreeningDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runId: str
    queryFastaS3Uri: str = Field(min_length=1)
    targetFastaS3Uri: str = Field(min_length=1)


class InteractionScreeningDatasetUploadResponse(DatasetUploadResponse):
    """Dataset upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str


class InteractionScreeningS3UploadResponse(S3DatasetUploadResponse):
    """S3 upload response for interaction-screening — splitOutputDir is always present."""

    splitOutputDir: str
