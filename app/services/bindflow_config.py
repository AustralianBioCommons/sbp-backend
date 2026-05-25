"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any

from ._nf_config import GADI_TRACE_SECTION, Raw, Section, build_nf_config


def get_bindflow_default_params(out_dir: str) -> dict[str, Any]:
    """Get default parameters for bindflow workflow."""
    return {
        "use_dgxa100": False,
        "validate_params": True,
        "help_full": False,
        "custom_config_base": "https://raw.githubusercontent.com/nf-core/configs/master",
        "show_hidden": False,
        "plaintext_email": False,
        "project": "yz52",
        "monochrome_logs": False,
        "error_strategy": "terminate",
        "version": False,
        "custom_config_version": "master",
        "outdir": out_dir,
        "quote_char": '"',
        "bindcraft_container": "australianbiocommons/freebindcraft:1.0.3",
        "publish_dir_mode": "copy",
        "pipelines_testdata_base_path": "https://raw.githubusercontent.com/nf-core/test-datasets/",
        "batches": 1,
        "help": False,
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
    job_id: str,
    user_email: str,
    timestamp: str,
    full_name: str = "",
    institute: str = "",
    ip_address: str = "",
) -> str:
    """Get Nextflow configText for the Seqera launch payload."""
    return build_nf_config(
        Section(
            name="singularity",
            entries={
                "cacheDir": "/g/data/if89/singularity_cache/",
                "enabled": True,
                "runOptions": "--nv",
                "autoMounts": True,
            },
        ),
        Section(
            name="process",
            entries={
                "executor": "pbspro",
                "clusterOptions": (
                    f"-v JOB_ID={job_id},USER_NAME={user_email},TIMESTAMP={timestamp}"
                    f",FULL_NAME={full_name},INSTITUTE={institute},IP_ADDRESS={ip_address}"
                ),
                "storage": "scratch/yz52+gdata/yz52+gdata/if89+gdata/li87",
                "shell": ["bash", "-C", "-e", "-u", "-o", "pipefail"],
                "withName: 'BINDCRAFT'": {
                    "queue": Raw('{ params.use_dgxa100 ? "dgxa100" : "gpuvolta" }'),
                    "cpus": Raw("{ params.use_dgxa100 ? 16 : 12 }"),
                    "gpus": 1,
                    "memory": "24.GB",
                    "time": "24.h",
                },
            },
        ),
        Section(
            name="executor",
            entries={
                "queueSize": 300,
                "pollInterval": "5 min",
                "queueStatInterval": "5 min",
                "submitRateLimit": "20 min",
            },
        ),
        GADI_TRACE_SECTION,
    )
