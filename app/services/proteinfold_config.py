"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""
from __future__ import annotations

def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> list[str]:
    """Get default parameters for proteinfold workflow."""
    return [
        f'outdir: "{out_dir}"',
        f'input: "{samplesheet_url}"',
        'db: "/g/data/if89/proteinfold_dbs/proteinfold_minidbs/"',
        'project: "yz52"',
        f'mode: "{mode}"',
        'use_gpu: true',
    ]

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
