"""ProteinDJ workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows.shared import WorkflowUserDetails
from .workflow_config_fetcher import fetch_workflow_config


def get_proteindj_design_mode(tool: str) -> str:
    """Map the de-novo-design tool selection to a ProteinDJ design_mode.

    BindCraft uses ProteinDJ's own bindcraft_denovo fold-design mode;
    everything else (rfdiffusion) uses standalone binder design.
    """
    return "bindcraft_denovo" if tool.lower() == "bindcraft" else "rfd_denovo"


def get_proteindj_default_params(
    out_dir: str,
    input_pdb: str,
    hotspot_residues: str,
    num_designs: int,
    design_length: str,
    design_mode: str,
) -> dict[str, Any]:
    """Get default parameters for proteindj workflow.

    ProteinDJ takes a single PDB plus design params directly — no
    samplesheet — so these are passed straight through as paramsText keys.
    Serves both the rfdiffusion and bindcraft tools; only design_mode differs.
    """
    return {
        "out_dir": out_dir,
        "input_pdb": input_pdb,
        "hotspot_residues": hotspot_residues,
        "num_designs": num_designs,
        "design_length": design_length,
        "design_mode": design_mode,
    }


def get_proteindj_config_profiles() -> list[str]:
    """Get config profiles for proteindj workflow."""
    return ["singularity"]


def get_proteindj_config_text(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
) -> str:
    """Read proteindj base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    cluster_opts = f"-A {user_details.get_encoded_account_details()}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
