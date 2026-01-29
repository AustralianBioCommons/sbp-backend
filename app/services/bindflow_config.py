"""Bindflow workflow configuration and executor settings."""

from __future__ import annotations


def get_bindflow_default_params(out_dir: str) -> list[str]:
    """Get default parameters for bindflow workflow."""
    return [
        "use_dgxa100: false",
        "validate_params: true",
        "help_full: false",
        'custom_config_base: "https://raw.githubusercontent.com/nf-core/configs/master"',
        "show_hidden: false",
        "plaintext_email: false",
        'project: "yz52"',
        "monochrome_logs: false",
        'error_strategy: "terminate"',
        "version: false",
        'custom_config_version: "master"',
        f'outdir: "{out_dir}"',
        "quote_char: '\"'",
        'bindcraft_container: "australianbiocommons/freebindcraft:1.0.3"',
        'publish_dir_mode: "copy"',
        'pipelines_testdata_base_path: "https://raw.githubusercontent.com/nf-core/test-datasets/"',
        "batches: 1",
        "help: false",
    ]


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
