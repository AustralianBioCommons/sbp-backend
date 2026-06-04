"""WISPS (Interaction Screening) workflow configuration."""

from __future__ import annotations

from typing import Any

_AWK = (
    'awk -v o="$D" \'/^>/{if(f)close(f);'
    'match($0,/^>([^ \\t]+)/,a);f=o"/"a[1]".fasta"}'
    "{print>f}' /tmp/w.fa"
)


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
) -> str:
    """Pre-run script for WISPS workflow on Gadi.

    Downloads the combined FASTA from S3 then splits it into per-sequence files
    under split_output_dir before the workflow begins.
    """
    s3_path = fasta_s3_uri.replace("s3://", "", 1)
    lines = [
        "module load singularity",
        "module load nextflow",
        f"export AWS_ACCESS_KEY_ID={aws_access_key}",
        f"export AWS_SECRET_ACCESS_KEY={aws_secret_key}",
        f"export AWS_REGION={aws_region}",
        "export RCLONE_S3_PROVIDER=AWS",
        "export RCLONE_S3_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID",
        "export RCLONE_S3_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY",
        "export RCLONE_S3_REGION=$AWS_REGION",
        f'rclone copyto ":s3:{s3_path}" /tmp/w.fa',
        f'D="{split_output_dir}"',
        'mkdir -p "$D"',
        _AWK,
    ]
    return "\n".join(lines) + "\n"


def get_wisps_config_profiles() -> list[str]:
    return ["singularity"]


def get_wisps_config_text(
    config_file_path: str,
    job_id: str = "",
    username: str = "",
    timestamp: str = "",
    full_name: str = "",
    institute: str = "",
    ip_address: str = "",
) -> str:
    """Read wisps config and append a process override block with runtime values.

    clusterOptions uses ${params.xxx} in the config file, but Nextflow evaluates
    those at config-parse time — before params are resolved — so unknown params
    cause 'Unknown config attribute' errors. Appending a process block with the
    values embedded directly overrides the file's clusterOptions.
    """
    if config_file_path.startswith(("http://", "https://")):
        import httpx

        response = httpx.get(config_file_path, timeout=30, follow_redirects=True)
        response.raise_for_status()
        base = response.text
    else:
        with open(config_file_path) as f:
            base = f.read()

    cluster_opts = (
        f"-P yz52 -v JOB_ID={job_id},USER_NAME={username},"
        f"TIMESTAMP={timestamp},FULL_NAME={full_name},"
        f"INSTITUTE={institute},IP_ADDRESS={ip_address}"
    )
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
