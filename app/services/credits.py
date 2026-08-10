"""Credit-cost configuration and calculation for workflow executions.

Single source of truth for the credit multipliers described in the SBP
credit-calculation spec. The frontend fetches these multipliers (via the
``/credits`` endpoint) and computes a run's display cost locally; this module
provides the authoritative cost calculation used for deduction at launch. A
run's cost is ``tool_multiplier × quantity``, where the quantity is derived per
the workflow's ``basis``.

These initial multipliers may be slightly adjusted for production — keep this
module as the one place to edit them.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, Field

from ..schemas.workflows.shared import WorkflowName, WorkflowTool


def is_credits_enabled() -> bool:
    """Whether credit checking/deduction is active (env ``ENABLE_CREDITS``)."""
    return os.getenv("ENABLE_CREDITS", "false").strip().lower() in {"1", "true", "yes"}


# Standard SBP user credit allowance: the one-time bundle grant applied when a
# user's workflow-execution role is first approved, and the amount every
# user's balance is reset to on the monthly refresh.
SBP_USER_CREDIT_ALLOWANCE = 1000

# credit_updated_by labels for the two paths that apply SBP_USER_CREDIT_ALLOWANCE.
SBP_BUNDLE_CREDIT_ACTOR = "sbp bundle approval"
MONTHLY_CREDIT_REFRESH_ACTOR = "monthly credit refresh"


class CreditBasis(StrEnum):
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


def launch_credit_cost(category: str, tool: str, final_design_count: int | None) -> int | None:
    """Authoritative per-run cost for workflows charged server-side at launch.

    Only de-novo (final designs) and single (constant) are charged today — their
    quantity is fully determined by the launch payload. interaction/bulk are not
    charged here (display-only); they return None.
    """
    cat = category.strip().lower()
    if cat == "single-prediction":
        return compute_cost(cat, tool, 1)
    if cat == "de-novo-design":
        if final_design_count is None or final_design_count < 1:
            return None
        return compute_cost(cat, tool, final_design_count)
    return None
