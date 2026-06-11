"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from .workflow_config_fetcher import fetch_workflow_config


def get_bindflow_default_params(out_dir: str, samplesheet_url: str) -> dict[str, Any]:
    """Get default parameters for bindflow workflow."""
    return {
        "project": "yz52",
        "outdir": out_dir,
        "input": samplesheet_url,
    }


def get_bindflow_executor_script(
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "ap-southeast-2",
) -> str:
    """Get the executor pre-run script for bindflow workflow on Gadi."""
    return f"""module load singularity
module load nextflow
export AWS_ACCESS_KEY_ID={aws_access_key}
export AWS_SECRET_ACCESS_KEY={aws_secret_key}
export AWS_REGION={aws_region}
"""


def get_bindflow_config_profiles() -> list[str]:
    """Get config profiles for bindflow workflow."""
    return ["singularity", "gadi"]


def get_bindflow_config_text(
    config_file_path: str,
    *,
    job_id: str,
    username: str,
    timestamp: str,
    full_name: str = "",
    institute: str = "",
    ip_address: str = "",
) -> str:
    """Read bindflow base config and append a process override block with runtime values."""
    base = fetch_workflow_config(config_file_path)

    cluster_opts = (
        f"-v JOB_ID={job_id},USER_NAME={username},"
        f"TIMESTAMP={timestamp},FULL_NAME={full_name},"
        f"INSTITUTE={institute},IP_ADDRESS={ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
