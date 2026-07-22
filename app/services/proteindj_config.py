"""ProteinDJ workflow configuration and executor settings (modeled after bindflow)."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .cluster_utils import encode_ip
from .workflow_config_fetcher import fetch_workflow_config


def get_proteindj_default_params(
    out_dir: str,
    input_pdb: str,
    hotspot_residues: str,
    num_designs: int,
    design_length: str,
) -> dict[str, Any]:
    """Get default parameters for proteindj workflow.

    ProteinDJ (rfdiffusion) takes a single PDB plus design params directly —
    no samplesheet — so these are passed straight through as paramsText keys.
    """
    return {
        "outdir": out_dir,
        "input_pdb": input_pdb,
        "hotspot_residues": hotspot_residues,
        "num_designs": num_designs,
        "design_length": design_length,
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

    account = (
        f"{user_details.user_email}:{encode_ip(user_details.ip_address)}"
        if user_details.ip_address
        else user_details.user_email
    )
    cluster_opts = f"-A {account}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
