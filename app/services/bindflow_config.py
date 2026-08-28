"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows.shared import WorkflowUserDetails
from .workflow_config_fetcher import fetch_workflow_config


def get_bindflow_default_params(
    out_dir: str, samplesheet_url: str, gadi_project: str = "yz52"
) -> dict[str, Any]:
    """Get default parameters for bindflow workflow."""
    return {
        "project": gadi_project,
        "outdir": out_dir,
        "input": samplesheet_url,
    }


def get_bindflow_config_profiles() -> list[str]:
    """Get config profiles for bindflow workflow."""
    return ["singularity"]


def get_bindflow_config_text(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
) -> str:
    """Read bindflow base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    cluster_opts = f"-A {user_details.get_encoded_account_details()}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
