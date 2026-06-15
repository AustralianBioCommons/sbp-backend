"""Credit-cost configuration and calculation for workflow executions.

Single source of truth for the credit multipliers described in the SBP
credit-calculation spec, and the authoritative cost calculation used both for
the estimate endpoint (display) and for deduction at launch. A run's cost is
``tool_multiplier × quantity``, where the quantity is derived per the workflow's
``basis``.

These initial multipliers may be slightly adjusted for production — keep this
module as the one place to edit them.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import cast

from pydantic import BaseModel, Field

from ..schemas.workflows import WorkflowName, WorkflowTool


def is_credits_enabled() -> bool:
    """Whether credit checking/deduction is active (env ``ENABLE_CREDITS``)."""
    return os.getenv("ENABLE_CREDITS", "false").strip().lower() in {"1", "true", "yes"}


class CreditBasis(str, Enum):
    """Which input quantity drives a workflow's credit cost.

    The frontend computes ``credits = tool_multiplier * quantity``, where
    ``quantity`` is derived per the basis below.
    """

    # Number of final designs produced (de novo design).
    FINAL_DESIGN_COUNT = "final_design_count"
    # Always 1 — a single prediction.
    CONSTANT = "constant"
    # Number of entries in the FASTA input (bulk prediction).
    FASTA_ENTRY_COUNT = "fasta_entry_count"
    # Product of the entry counts of the two FASTA inputs (interaction screening).
    FASTA_PAIR_PRODUCT = "fasta_pair_product"


class WorkflowCreditConfig(BaseModel):
    """Credit-cost rules for a single workflow category.

    The frontend computes a run's cost as ``tool_multiplier * quantity``, where
    the tool multiplier is looked up in ``toolMultipliers`` and ``quantity`` is
    derived per ``basis``.
    """

    category: WorkflowName = Field(..., description="Workflow category slug, e.g. 'de-novo-design'")
    displayName: str = Field(..., description="Human-readable category name")
    basis: CreditBasis = Field(..., description="Which input quantity drives the cost")
    toolMultipliers: dict[WorkflowTool, int] = Field(
        ..., description="Per-tool credit multiplier, keyed by tool id"
    )


class WorkflowCreditsResponse(BaseModel):
    """Credit-cost rules for every workflow category."""

    workflows: list[WorkflowCreditConfig] = Field(default_factory=list)


# Source of truth — mirrors the SBP credit-calculation spec
# available at https://biocloud.atlassian.net/wiki/spaces/SBP/pages/748584961/SBP+credit+calculation
_WORKFLOW_CREDIT_CONFIGS: tuple[WorkflowCreditConfig, ...] = (
    WorkflowCreditConfig(
        category="de-novo-design",
        displayName="De novo Design",
        basis=CreditBasis.FINAL_DESIGN_COUNT,
        toolMultipliers={"bindcraft": 20, "rfdiffusion": 10},
    ),
    WorkflowCreditConfig(
        category="single-prediction",
        displayName="Single Prediction",
        basis=CreditBasis.CONSTANT,
        toolMultipliers={"boltz": 1, "colabfold": 5, "alphafold2": 5},
    ),
    WorkflowCreditConfig(
        category="bulk-prediction",
        displayName="Bulk Prediction",
        basis=CreditBasis.FASTA_ENTRY_COUNT,
        toolMultipliers={"boltz": 1, "colabfold": 1},
    ),
    WorkflowCreditConfig(
        category="interaction-screening",
        displayName="Interaction Screening",
        basis=CreditBasis.FASTA_PAIR_PRODUCT,
        toolMultipliers={"boltz": 1, "colabfold": 1},
    ),
)

_CONFIGS_BY_CATEGORY: dict[WorkflowName, WorkflowCreditConfig] = {
    config.category: config for config in _WORKFLOW_CREDIT_CONFIGS
}


def list_workflow_credit_configs() -> tuple[WorkflowCreditConfig, ...]:
    """Return the credit-cost rules for every workflow category."""
    return _WORKFLOW_CREDIT_CONFIGS


def get_workflow_credit_config(category: str) -> WorkflowCreditConfig | None:
    """Return the credit-cost rules for a single workflow category, if known."""
    return _CONFIGS_BY_CATEGORY.get(cast(WorkflowName, category.strip().lower()))


def count_fasta_entries(content: str | None) -> int:
    """Count the number of records in a (multi-)FASTA string (lines starting '>')."""
    if not content:
        return 0
    return sum(1 for line in content.splitlines() if line.lstrip().startswith(">"))


def get_tool_multiplier(category: str, tool: str) -> int | None:
    """Return the per-tool credit multiplier for a workflow category, if known."""
    config = get_workflow_credit_config(category)
    if config is None:
        return None
    return config.toolMultipliers.get(cast(WorkflowTool, tool.strip().lower()))


def compute_cost(category: str, tool: str, quantity: int) -> int | None:
    """Compute a run's credit cost as ``tool_multiplier × quantity``.

    Returns None when the category/tool has no configured multiplier (caller
    decides how to treat an uncosted run). ``quantity`` is clamped to ``>= 0``.
    """
    multiplier = get_tool_multiplier(category, tool)
    if multiplier is None:
        return None
    return multiplier * max(0, quantity)
