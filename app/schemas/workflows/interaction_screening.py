"""Schemas for the interaction-screening workflow.

``WispsFormData``/``BulkPredictionDatasetUploadRequest`` back bulk-prediction
(see ``bulk_prediction.py``), whose samplesheet has a single row pointing at one
multi-FASTA. Interaction screening instead uploads one query and one target
FASTA, and its samplesheet has exactly two rows (g1=query, g2=target) pointing
at those files. In both cases the pipeline splits each multi-FASTA itself
(``multi_fasta_file``), so there is no prerun split.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .shared import DatasetUploadResponse, S3DatasetUploadResponse, WorkflowFormData


class WispsFormData(WorkflowFormData):
    """Form data for bulk-prediction."""

    fastaS3Uri: str = Field(..., description="S3 URI of the multi-FASTA file to predict")


class BulkPredictionDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runId: str
    fastaS3Uri: str = Field(min_length=1)


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
