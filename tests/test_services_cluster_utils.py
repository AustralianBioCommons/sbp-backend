"""Tests for cluster_utils.py."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

import pytest

from app.schemas.workflows.shared import WorkflowUserDetails
from app.services import cluster_utils
from app.services.proteindj_config import get_proteindj_config_text
from app.services.proteinfold_config import get_proteinfold_config_text
from app.services.wisps_config import get_wisps_config_text


def test_get_gadi_project_returns_settings_value(mock_settings):
    mock_settings.seqera.gadi_project = "ab12"

    assert cluster_utils.get_gadi_project(mock_settings) == "ab12"


@pytest.mark.parametrize("ip_address", ["1.2.3.4", ""])
def test_append_process_config_preserves_base_and_accounts_for_every_selector(ip_address):
    user = WorkflowUserDetails(user_email="user@example.com", ip_address=ip_address)
    base = "process { executor = 'pbspro' }"
    result = cluster_utils.append_process_config(
        base,
        user_details=user,
        gadi_project="ab12",
        process_options={"SEARCH": "-l iointensive=2", "PIPELINE:ALIGN": "-l jobfs=10GB"},
    )
    account = user.get_encoded_account_details()

    assert result == base + (
        f'\nprocess {{\n    clusterOptions = "-P ab12 -A {account}"\n'
        "\n    withName: 'SEARCH' {\n"
        f'        clusterOptions = "-l iointensive=2 -P ab12 -A {account}"\n'
        "    }\n"
        "\n    withName: 'PIPELINE:ALIGN' {\n"
        f'        clusterOptions = "-l jobfs=10GB -P ab12 -A {account}"\n'
        "    }\n"
        "}\n"
    )
    assert user.user_email not in result
    if ip_address:
        assert ip_address not in result


@pytest.mark.parametrize("process_options", [None, {}])
def test_append_process_config_without_selectors(process_options):
    user = WorkflowUserDetails(user_email="user@example.com", ip_address="")

    result = cluster_utils.append_process_config(
        "base", user_details=user, gadi_project="ab12", process_options=process_options
    )

    assert result == (
        f'base\nprocess {{\n    clusterOptions = "-P ab12 -A {user.get_encoded_account_details()}"\n}}\n'
    )


@pytest.mark.parametrize("get_config", [get_proteinfold_config_text, get_wisps_config_text])
def test_colabfold_workflows_append_resources_and_runtime_accounting(tmp_path, get_config):
    config = tmp_path / "workflow.config"
    base = "process { executor = 'pbspro' }\n"
    config.write_text(base)
    user = WorkflowUserDetails(user_email="user@example.com", ip_address="1.2.3.4")

    result = get_config(str(config), user_details=user, gadi_project="ab12")

    required = f"-P ab12 -A {user.get_encoded_account_details()}"
    assert result == base + (
        f'\nprocess {{\n    clusterOptions = "{required}"\n'
        "\n    withName: 'MMSEQS_COLABFOLDSEARCH' {\n"
        '        clusterOptions = "-l iointensive=2,ncpus=14,mem=128GB,'
        f'storage=gdata/if89+gdata/li87+gdata/yz52+scratch/yz52 {required}"\n'
        "    }\n"
        "}\n"
    )


def test_proteindj_config_includes_project_and_account(tmp_path):
    config = tmp_path / "workflow.config"
    config.write_text("base")
    user = WorkflowUserDetails(user_email="user@example.com", ip_address="1.2.3.4")

    result = get_proteindj_config_text(str(config), user_details=user, gadi_project="ab12")

    assert result == (
        f'base\nprocess {{\n    clusterOptions = "-P ab12 -A {user.get_encoded_account_details()}"\n}}\n'
    )
