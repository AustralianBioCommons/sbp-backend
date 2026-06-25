"""Helpers for safely preparing Seqera launch payloads."""

from __future__ import annotations

import shlex
from typing import Any

from .workflow_config_fetcher import fetch_workflow_config


def without_prerun_script(launch_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy safe to persist in the job queue."""
    persisted_payload = launch_payload.copy()
    persisted_payload.pop("preRunScript", None)
    return persisted_payload


def inject_prerun_script(
    launch_payload: dict[str, Any],
    prerun_script: str,
) -> dict[str, Any]:
    """Return a launch-time payload with preRunScript generated at send time."""
    runtime_payload = launch_payload.copy()
    runtime_payload["preRunScript"] = prerun_script
    return runtime_payload


def get_executor_script(
    *,
    prerun_script_path: str | None,
    env: dict[str, str],
    module_loads: list[str] | None = None,
) -> str:
    """Build a pre-run script from module loads, environment exports, and a script body."""
    lines = []

    for module in module_loads or []:
        lines.append(f"module load {module}")

    for key, value in env.items():
        lines.append(f"export {key}={shlex.quote(value)}")

    header = "\n".join(lines) + "\n"
    body = fetch_workflow_config(prerun_script_path) if prerun_script_path else ""
    return header + body
