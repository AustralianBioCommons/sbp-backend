"""Schemas for the de-novo-design workflow.

Both the rfdiffusion and bindcraft tools launch ProteinDJ (see
``proteindj_executor.py``) and share this same form shape - only the
design_mode passed to the pipeline differs between them.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from .shared import WorkflowFormData

MAX_HOTSPOT_RESIDUES = 8
MIN_DESIGN_LENGTH = 65
MAX_DESIGN_LENGTH = 150


class ProteinDjFormData(WorkflowFormData):
    """Form data for the ProteinDJ de-novo-design workflow (rfdiffusion or bindcraft tool)."""

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
