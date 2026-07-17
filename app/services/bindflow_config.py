"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .cluster_utils import GADI_PROJECT, encode_ip
from .workflow_config_fetcher import fetch_workflow_config


def get_bindflow_default_params(out_dir: str, samplesheet_url: str) -> dict[str, Any]:
    """Get default parameters for bindflow workflow."""
    return {
        "project": GADI_PROJECT,
        "outdir": out_dir,
        "input": samplesheet_url,
    }


def get_bindflow_config_profiles() -> list[str]:
    """Get config profiles for bindflow workflow."""
    return ["singularity", "gadi"]


def get_bindflow_config_text(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
) -> str:
    """Read bindflow base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    account = (
        f"{user_details.user_email}:{encode_ip(user_details.ip_address)}"
        if user_details.ip_address
        else user_details.user_email
    )
    cluster_opts = f"-A {account}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
