"""Utility for loading workflow config files from a local path or a remote URL."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx

from ..schemas.workflows.shared import WorkflowUserDetails


def validate_config_path(path_or_url: str) -> None:
    if not path_or_url or not path_or_url.strip():
        raise ValueError("config_path must not be empty")

    if not path_or_url.startswith(("http://", "https://")):
        return

    parsed = urlparse(path_or_url)

    if not parsed.netloc:
        raise ValueError(f"config_path URL has no host: {path_or_url!r}")

    if "token" in parse_qs(parsed.query):
        raise ValueError(
            "config_path URL must not contain a 'token' query parameter — "
            "store a clean URL in the database and configure authentication separately"
        )


def fetch_workflow_config(path_or_url: str) -> str:
    """Return the text content of a workflow config from a local path or URL."""
    validate_config_path(path_or_url)

    if path_or_url.startswith(("http://", "https://")):
        response = httpx.get(path_or_url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return response.text

    with open(path_or_url) as f:
        return f.read()


def render_workflow_config(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
    gadi_project: str | None = None,
    pbs_enabled: bool = True,
) -> str:
    """Load workflow config and append PBS project/account options when enabled."""
    base = fetch_workflow_config(config_file_path)
    if not pbs_enabled:
        return base

    cluster_opts = (
        f"-P {gadi_project} " if gadi_project else ""
    ) + f"-A {user_details.get_encoded_account_details()}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
