"""Proteinfold workflow configuration and executor settings (modeled after bindflow)."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .cluster_utils import GADI_PROJECT, encode_value
from .workflow_config_fetcher import fetch_workflow_config


def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> dict[str, Any]:
    """Get default parameters for proteinfold workflow."""
    return {"input": samplesheet_url, "outdir": out_dir, "project": GADI_PROJECT, "mode": mode}


def get_proteinfold_config_profiles() -> list[str]:
    """Get config profiles for proteinfold workflow."""
    return ["singularity"]


def get_proteinfold_config_text(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
) -> str:
    """Read proteinfold base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    account = (
        f"{encode_value(user_details.user_email)}:{encode_value(user_details.ip_address)}"
        if user_details.ip_address
        else encode_value(user_details.user_email)
    )
    cluster_opts = f"-P {GADI_PROJECT} -A {account}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
