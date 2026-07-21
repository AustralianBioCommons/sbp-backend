"""ProteinDJ workflow configuration and executor settings (modeled after bindflow)."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .cluster_utils import encode_ip
from .workflow_config_fetcher import fetch_workflow_config


def get_proteindj_default_params(
    out_dir: str,
    input_pdb: str | None = None,
    hotspot_residues: str | None = None,
    num_designs: int | None = None,
    design_length: str | None = None,
) -> dict[str, Any]:
    """Get default parameters for proteindj workflow.

    ProteinDJ (rfdiffusion) takes a single PDB plus design params directly —
    no samplesheet — so these are passed straight through as paramsText keys.
    """
    params: dict[str, Any] = {"out_dir": out_dir}
    if input_pdb is not None:
        params["input_pdb"] = input_pdb
    if hotspot_residues is not None:
        params["hotspot_residues"] = hotspot_residues
    if num_designs is not None:
        params["num_designs"] = num_designs
    if design_length is not None:
        params["design_length"] = design_length
    return params


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
