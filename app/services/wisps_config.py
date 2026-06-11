"""WISPS (Interaction Screening) workflow configuration."""

from __future__ import annotations

from typing import Any

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


def get_wisps_executor_script(
    fasta_s3_uri: str,
    split_output_dir: str,
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "ap-southeast-2",
    prerun_script_path: str | None = None,
) -> str:
    """Pre-run script for WISPS workflow on Gadi.

    Builds a variable-assignment header with dynamic values and appends the
    script body fetched from prerun_script_path.
    """
    s3_path = fasta_s3_uri.replace("s3://", "", 1)
    header = (
        "\n".join(
            [
                f"AWS_ACCESS_KEY_ID={aws_access_key}",
                f"AWS_SECRET_ACCESS_KEY={aws_secret_key}",
                f"AWS_REGION={aws_region}",
                f"S3_PATH={s3_path}",
                f'D="{split_output_dir}"',
            ]
        )
        + "\n"
    )

    body = fetch_workflow_config(prerun_script_path) if prerun_script_path else ""
    return header + body


def get_wisps_config_profiles() -> list[str]:
    return ["singularity"]


def get_wisps_config_text(
    config_file_path: str,
    *,
    job_id: str,
    username: str,
    timestamp: str,
    full_name: str = "",
    institute: str = "",
    ip_address: str = "",
) -> str:
    """Read wisps config and append a process override block with runtime values.

    Appending a process block with the
    values embedded directly overrides the file's clusterOptions.
    """
    base = fetch_workflow_config(config_file_path)

    cluster_opts = (
        f"-P yz52 -v JOB_ID={job_id},USER_NAME={username},"
        f"TIMESTAMP={timestamp},FULL_NAME={full_name},"
        f"INSTITUTE={institute},IP_ADDRESS={ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
