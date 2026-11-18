import pytest

from app.schemas.workflows.shared import WorkflowUserDetails
from app.services.workflow_config_fetcher import render_workflow_config


@pytest.mark.parametrize("project", [None, "backend-project"])
@pytest.mark.parametrize("ip_address", ["", "1.2.3.4"])
def test_render_workflow_config_appends_pbs_options(tmp_path, project, ip_address):
    config = tmp_path / "workflow.config"
    config.write_text("// base configuration\n")

    result = render_workflow_config(
        str(config),
        user_details=WorkflowUserDetails(user_email="user@ex.com", ip_address=ip_address),
        gadi_project=project,
    )

    account = "dXNlckBleC5jb20=" + (":MS4yLjMuNA==" if ip_address else "")
    options = (f"-P {project} " if project else "") + f"-A {account}"
    assert result == (
        "// base configuration\n\nprocess {\n" f'    clusterOptions = "{options}"\n' "}\n"
    )


def test_render_workflow_config_without_pbs_preserves_base(tmp_path):
    config = tmp_path / "workflow.config"
    config.write_text('// base configuration\nprocess.executor = "awsbatch"\n')

    result = render_workflow_config(
        str(config),
        user_details=WorkflowUserDetails(user_email="user@ex.com", ip_address="1.2.3.4"),
        gadi_project="backend-project",
        pbs_enabled=False,
    )

    assert result == config.read_text()
