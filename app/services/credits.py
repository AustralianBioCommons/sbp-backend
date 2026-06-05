"""Credit-cost configuration for workflow executions.

Single source of truth for the credit multipliers described in the SBP
credit-calculation spec. The actual per-run cost is computed in the frontend;
the backend simply exposes the per-tool multipliers (and a ``basis`` hint for
which input quantity drives the cost) so the frontend can derive a cost.

These initial multipliers may be slightly adjusted for production — keep this
module as the one place to edit them.
"""

from __future__ import annotations

from enum import Enum


class CreditBasis(str, Enum):
    """How the variable quantity in a credit formula is derived.

    ``credits = tool_multiplier * quantity`` where ``quantity`` depends on the
    basis below.
    """

    # Number of final designs produced (de novo design).
    FINAL_DESIGN_COUNT = "final_design_count"
    # Always 1 — a single prediction.
    CONSTANT = "constant"
    # Number of entries in the FASTA input (bulk prediction).
    FASTA_ENTRY_COUNT = "fasta_entry_count"
    # Product of the entry counts of the two FASTA inputs (interaction screening).
    FASTA_PAIR_PRODUCT = "fasta_pair_product"


class WorkflowCreditConfig:
    """Credit-cost rules for a single workflow category.

    The arithmetic (``tool_multiplier * quantity``) lives in the frontend; this
    only exposes the per-tool multipliers and the ``basis`` hint describing which
    input quantity drives the cost.
    """

    def __init__(
        self,
        category: str,
        display_name: str,
        basis: CreditBasis,
        tool_multipliers: dict[str, int],
    ) -> None:
        self.category = category
        self.display_name = display_name
        self.basis = basis
        self.tool_multipliers = tool_multipliers


# Source of truth — mirrors the SBP credit-calculation spec
# available at https://biocloud.atlassian.net/wiki/spaces/SBP/pages/748584961/SBP+credit+calculation
_WORKFLOW_CREDIT_CONFIGS: tuple[WorkflowCreditConfig, ...] = (
    WorkflowCreditConfig(
        category="de-novo-design",
        display_name="De novo Design",
        basis=CreditBasis.FINAL_DESIGN_COUNT,
        tool_multipliers={"bindcraft": 20, "rfdiffusion": 10},
    ),
    WorkflowCreditConfig(
        category="single-prediction",
        display_name="Single Prediction",
        basis=CreditBasis.CONSTANT,
        tool_multipliers={"boltz": 1, "colabfold": 5, "alphafold2": 5},
    ),
    WorkflowCreditConfig(
        category="bulk-prediction",
        display_name="Bulk Prediction",
        basis=CreditBasis.FASTA_ENTRY_COUNT,
        tool_multipliers={"boltz": 1, "colabfold": 1},
    ),
    WorkflowCreditConfig(
        category="interaction-screening",
        display_name="Interaction Screening",
        basis=CreditBasis.FASTA_PAIR_PRODUCT,
        tool_multipliers={"boltz": 1, "colabfold": 1},
    ),
)

_CONFIGS_BY_CATEGORY: dict[str, WorkflowCreditConfig] = {
    config.category: config for config in _WORKFLOW_CREDIT_CONFIGS
}


def list_workflow_credit_configs() -> tuple[WorkflowCreditConfig, ...]:
    """Return the credit-cost rules for every workflow category."""
    return _WORKFLOW_CREDIT_CONFIGS


def get_workflow_credit_config(category: str) -> WorkflowCreditConfig | None:
    """Return the credit-cost rules for a single workflow category, if known."""
    return _CONFIGS_BY_CATEGORY.get(category.strip().lower())
