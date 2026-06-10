"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""

from __future__ import annotations

from typing import Any

import httpx


def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> dict[str, Any]:
    """Get default parameters for proteinfold workflow."""
    return {"input": samplesheet_url, "outdir": out_dir, "project": "yz52", "mode": mode}


def get_proteinfold_executor_script(
    aws_access_key: str = "", aws_secret_key: str = "", aws_region: str = "ap-southeast-2"
) -> str:
    """Get the executor pre-run script for proteinfold workflow on Gadi."""
    return f"""module load singularity
module load nextflow
export AWS_ACCESS_KEY_ID={aws_access_key}
export AWS_SECRET_ACCESS_KEY={aws_secret_key}
export AWS_REGION={aws_region}
"""


def get_proteinfold_config_profiles() -> list[str]:
    """Get config profiles for proteinfold workflow."""
    return ["singularity"]


def get_proteinfold_config_text(
    config_file_path: str,
    *,
    job_id: str,
    user_name: str,
    timestamp: str,
    full_name: str = "",
    institute: str = "",
    ip_address: str = "",
) -> str:
    """Read proteinfold base config and append a process override block with runtime values."""
    if config_file_path.startswith(("http://", "https://")):
        response = httpx.get(config_file_path, timeout=30, follow_redirects=True)
        response.raise_for_status()
        base = response.text
    else:
        with open(config_file_path, encoding="utf-8") as fh:
            base = fh.read()

    cluster_opts = (
        f"-P yz52 -v JOB_ID={job_id},USER_NAME={user_name},"
        f"TIMESTAMP={timestamp},FULL_NAME={full_name},"
        f"INSTITUTE={institute},IP_ADDRESS={ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
