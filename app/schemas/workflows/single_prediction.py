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

# DNA/RNA alphabets, consistent with the frontend's fasta.utils.ts. Protein
# sequences are validated with RDKit's own residue parser instead (see
# is_valid_protein_sequence) rather than a hand-rolled amino-acid regex.
_DNA_REGEX = re.compile(r"^[ATGC]+$")
_RNA_REGEX = re.compile(r"^[AUGC]+$")
_CCD_CODE_REGEX = re.compile(r"^[A-Z0-9]{1,5}$")

# Supported Chemical Component Dictionary (CCD) ligand codes, standardised by
# the wwPDB (https://www.wwpdb.org/data/ccd). Kept in sync with the frontend's
# CCD_COMPOUNDS in fasta.utils.ts — this list of 20 compounds matches what's
# supported by the standard AlphaFold Server.
CCD_COMPOUNDS: dict[str, str] = {
    "ADP": "Adenosine diphosphate",
    "ATP": "Adenosine triphosphate",
    "AMP": "Adenosine phosphate",
    "GTP": "Guanosine-5'-triphosphate",
    "GDP": "Guanosine-5'-diphosphate",
    "FAD": "Flavin-adenine dinucleotide",
    "NAD": "Nicotinamide-adenine-dinucleotide",
    "NAP": "Nicotinamide-adenine-dinucleotide-phosphate (NADP)",
    "NDP": "Dihydro-nicotinamide-adenine-dinucleotide-phosphate (NADPH)",
    "HEM": "Heme",
    "HEC": "Heme C",
    "PLM": "Palmitic acid",
    "OLA": "Oleic acid",
    "MYR": "Myristic acid",
    "CIT": "Citric acid",
    "CLA": "Chlorophyll A",
    "CHL": "Chlorophyll B",
    "BCL": "Bacteriochlorophyll A",
    "BCB": "Bacteriochlorophyll B",
}


class SinglePredictionEntity(BaseModel):
    """A single entity row submitted for a single-prediction run."""

    model_config = ConfigDict(extra="allow")

    id: NonEmptyStr | None = None
    moleculeType: MoleculeType
    copyNumber: PositiveInt = 1
    sequence: Annotated[str, StringConstraints(strip_whitespace=True)] = ""


def _normalize_sequence(value: str) -> str:
    """Strip all whitespace and uppercase, matching the frontend's normalization."""
    return re.sub(r"\s+", "", value or "").upper()


def is_valid_protein_sequence(value: str) -> bool:
    """Check a sequence is a valid protein, using RDKit's peptide sequence
    parser (flavor=0: L-amino acids); rejects ambiguous/extended one-letter
    codes (B, J, O, U, X, Z, ...) that aren't one of the 20 canonical residues."""
    normalized = _normalize_sequence(value)
    return bool(normalized) and Chem.MolFromSequence(normalized, flavor=0) is not None


def is_valid_dna_sequence(value: str) -> bool:
    """Check a sequence uses only valid DNA characters (A, T, G, C)."""
    normalized = _normalize_sequence(value)
    return bool(normalized) and bool(_DNA_REGEX.match(normalized))


def is_valid_rna_sequence(value: str) -> bool:
    """Check a sequence uses only valid RNA characters (A, U, G, C)."""
    normalized = _normalize_sequence(value)
    return bool(normalized) and bool(_RNA_REGEX.match(normalized))


def is_valid_ccd_code(value: str) -> bool:
    """Check a value is a supported wwPDB Chemical Component Dictionary code."""
    normalized = (value or "").strip().upper()
    return bool(_CCD_CODE_REGEX.match(normalized)) and normalized in CCD_COMPOUNDS


def is_valid_smiles(value: str) -> bool:
    """Check whether a string is a chemically valid SMILES, using RDKit."""
    return bool(value) and Chem.MolFromSmiles(value) is not None


def _validate_entity_sequence(entity: SinglePredictionEntity) -> None:
    """Validate an entity's sequence/code against the rules for its molecule type.

    Raises ``ValueError`` with a user-facing message when invalid.
    """
    label = entity.id or "unnamed entity"
    if entity.moleculeType == "protein":
        if not is_valid_protein_sequence(entity.sequence):
            raise ValueError(
                f"Protein entity '{label}' has an invalid sequence: only the 20 canonical "
                "amino acids (ARNDCQEGHILKMFPSTWYV) are allowed."
            )
    elif entity.moleculeType == "dna":
        if not is_valid_dna_sequence(entity.sequence):
            raise ValueError(
                f"DNA entity '{label}' has an invalid sequence: only valid DNA characters "
                "(A, T, G, C) are allowed."
            )
    elif entity.moleculeType == "rna":
        if not is_valid_rna_sequence(entity.sequence):
            raise ValueError(
                f"RNA entity '{label}' has an invalid sequence: only valid RNA characters "
                "(A, U, G, C) are allowed."
            )
    elif entity.moleculeType == "ccd":
        if not is_valid_ccd_code(entity.sequence):
            raise ValueError(
                f"CCD entity '{label}' has an unsupported ligand code: '{entity.sequence}'. "
                f"Supported codes: {', '.join(sorted(CCD_COMPOUNDS))}."
            )
    elif entity.moleculeType == "ligand" and not is_valid_smiles(entity.sequence):
        raise ValueError(
            f"Ligand entity '{label}' has an invalid SMILES string: '{entity.sequence}'"
        )


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
        _validate_entity_sequence(entity)
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
