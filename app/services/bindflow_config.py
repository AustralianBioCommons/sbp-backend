"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .workflow_config_fetcher import fetch_workflow_config


def get_bindflow_default_params(out_dir: str, samplesheet_url: str) -> dict[str, Any]:
    """Get default parameters for bindflow workflow."""
    return {
        "project": "yz52",
        "outdir": out_dir,
        "input": samplesheet_url,
    }


def get_bindflow_config_profiles() -> list[str]:
    """Get config profiles for bindflow workflow."""
    return ["singularity", "gadi"]


def get_bindflow_config_text(
    config_file_path: str,
    *,
    job_id: str,
    user_details: WorkflowUserDetails,
    timestamp: str,
) -> str:
    """Read bindflow base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    cluster_opts = (
        f"-v JOB_ID={job_id},USER_NAME={user_details.user_email},"
        f"TIMESTAMP={timestamp},FULL_NAME={user_details.full_name},"
        f"INSTITUTE={user_details.institute},IP_ADDRESS={user_details.ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
