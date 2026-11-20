"""Utilities for building HPC cluster option strings."""

from __future__ import annotations

from collections.abc import Mapping
from textwrap import dedent, indent

from ..config import Settings
from ..schemas.workflows.shared import WorkflowUserDetails

# Define any workflow/process specific clusteroptions here.
# These will also need to be backend-specific when we implement multiple backends
COLABFOLD_PROCESS_OPTIONS = {
    "MMSEQS_COLABFOLDSEARCH": (
        "-l iointensive=2,ncpus=14,mem=128GB,"
        "storage=gdata/if89+gdata/li87+gdata/yz52+scratch/yz52"
    ),
}


def get_gadi_project(settings: Settings) -> str:
    return settings.seqera.gadi_project


def append_process_config(
    base: str,
    *,
    user_details: WorkflowUserDetails,
    gadi_project: str,
    process_options: Mapping[str, str] | None = None,
) -> str:
    """
    Append required cluster options to the workflow config, along with
    workflow/process specific options.
    """
    required_options = f"-P {gadi_project} -A {user_details.get_encoded_account_details()}"

    override = dedent(f"""
        process {{
            clusterOptions = "{required_options}"
        """)
    for name, options in (process_options or {}).items():
        cluster_options = f"{options} {required_options}".strip()
        override += indent(
            dedent(f"""
                withName: '{name}' {{
                    clusterOptions = "{cluster_options}"
                }}
                """),
            "    ",
        )
    return base + override + "}\n"
