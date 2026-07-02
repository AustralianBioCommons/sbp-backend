"""WISPS (Interaction Screening) workflow configuration."""

from __future__ import annotations

from typing import Any

from ..schemas.workflows import WorkflowUserDetails
from .workflow_config_fetcher import fetch_workflow_config


def get_wisps_default_params(
    out_dir: str,
    samplesheet_url: str,
    tool: str | None = None,
) -> dict[str, Any]:
    """Params passed as YAML paramsText."""
    params: dict[str, Any] = {
        "outdir": out_dir,
        "input": samplesheet_url,
        "mode": "g1-g2",
    }
    if tool is not None:
        params["tools"] = tool
    return params


def get_wisps_config_profiles() -> list[str]:
    return ["singularity"]


def get_wisps_config_text(
    config_file_path: str,
    *,
    job_id: str,
    user_details: WorkflowUserDetails,
    timestamp: str,
) -> str:
    """Read wisps config and append a process override block with runtime values.

    Appending a process block with the
    values embedded directly overrides the file's clusterOptions.
    """
    base = fetch_workflow_config(config_file_path)

    cluster_opts = (
        f"-P yz52 -v JOB_ID={job_id},USER_NAME={user_details.user_email},"
        f"TIMESTAMP={timestamp},FULL_NAME={user_details.full_name},"
        f"INSTITUTE={user_details.institute},IP_ADDRESS={user_details.ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
