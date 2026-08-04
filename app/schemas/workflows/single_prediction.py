"""Schemas and validation for the single-prediction workflow."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints
from rdkit import Chem, RDLogger

# RDKit logs a warning/error to stderr for every unparseable SMILES by default;
# invalid SMILES are an expected user-input case here, not something to log.
RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MoleculeType = Literal["protein", "rna", "dna", "ligand", "ccd"]

SINGLE_PREDICTION_MAX_ENTITIES = 52
SINGLE_PREDICTION_LIGAND_SIZE = 30
SINGLE_PREDICTION_SIZE_LIMITS: dict[str, int] = {
    "alphafold2": 2000,
    "colabfold": 4000,
    "boltz": 4000,
}
SINGLE_PREDICTION_BOLTZ_POTENTIALS_SIZE_LIMIT = 2000


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
