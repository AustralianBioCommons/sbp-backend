from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.db.models import WorkflowRun
from app.routes.workflow.jobs import (
    bulk_delete_jobs,
    cancel_workflow,
    delete_job,
    get_job_details,
    list_jobs,
)
from app.routes.workflow.results import get_result_logs
from app.schemas.backend import ComputeBackendName
from app.schemas.workflows.shared import BulkDeleteJobsRequest
from app.services import job_utils
from app.services.job_sync import sync_workflow_run
from app.services.results_utils import WorkflowResultsSpec, get_output_spec
from tests.datagen import AppUserFactory, ComputeBackendFactory, WorkflowFactory, WorkflowRunFactory


@pytest.fixture(params=list(ComputeBackendName))
def backend_run(request, persistent_models):
    backend = ComputeBackendFactory.create_sync(
        name=request.param,
        seqera_workspace_id=f"{request.param}-workspace",
        service_usage_enabled=True,
    )
    return WorkflowRunFactory.create_sync(
        backend=backend,
        owner=AppUserFactory.create_sync(),
        workflow=WorkflowFactory.create_sync(name="single-prediction", tool="boltz"),
        tool="boltz",
        seqera_run_id="backend-run",
        seqera_final_status=None,
        sync_completed_at=None,
        submission_timestamp=datetime.now(UTC),
    )


@pytest.mark.parametrize("operation", ["list", "details", "logs", "cancel", "delete", "sync"])
async def test_run_operations_use_backend_workspace(test_db, mock_settings, backend_run, operation):
    run = backend_run
    workspace = run.backend.seqera_workspace_id
    assert workspace != mock_settings.seqera.work_space
    root = f"{mock_settings.seqera.api_url}/workflow/{run.seqera_run_id}"
    with respx.mock(assert_all_called=False) as router:
        describe = router.get(root, params={"workspaceId": workspace}).mock(
            return_value=httpx.Response(200, json={"workflow": {"status": "RUNNING"}})
        )
        logs = router.get(f"{root}/log", params={"workspaceId": workspace}).mock(
            return_value=httpx.Response(200, json={"entries": []})
        )
        cancel = router.post(f"{root}/cancel", params={"workspaceId": workspace}).mock(
            return_value=httpx.Response(200)
        )
        delete = router.delete(root, params={"workspaceId": workspace}).mock(
            return_value=httpx.Response(200)
        )
        if operation == "list":
            result = await list_jobs(
                search=None,
                status_filter=None,
                limit=50,
                offset=0,
                sort_by="submitted",
                sort_order="desc",
                current_user_id=run.owner_user_id,
                db=test_db,
                settings=mock_settings,
            )
            assert [item.id for item in result.jobs] == [str(run.id)]
        elif operation == "sync":
            result = await sync_workflow_run(test_db, run, settings=mock_settings)
            assert result.seqera_status == "RUNNING"
        else:
            action = {
                "details": get_job_details,
                "logs": get_result_logs,
                "cancel": cancel_workflow,
                "delete": delete_job,
            }[operation]
            await action(str(run.id), run.owner_user_id, test_db, mock_settings)
        assert describe.called == (operation in {"list", "details", "delete", "sync"})
        assert logs.called == (operation == "logs")
        assert cancel.called == (operation in {"cancel", "delete"})
        assert delete.called == (operation == "delete")


@pytest.mark.parametrize("failed_workspace", [None, "setonix-workspace"])
async def test_bulk_delete_groups_workspaces_and_isolates_failures(
    test_db, persistent_models, mock_settings, failed_workspace
):
    owner = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    runs = []
    batches = {}
    with respx.mock:
        for name in ComputeBackendName:
            workspace = f"{name}-workspace"
            backend = ComputeBackendFactory.create_sync(name=name, seqera_workspace_id=workspace)
            for index in range(2):
                run = WorkflowRunFactory.create_sync(
                    backend=backend,
                    owner=owner,
                    workflow=workflow,
                    seqera_run_id=f"{name}-{index}",
                )
                runs.append((run.id, run.seqera_run_id, workspace))
                root = f"{mock_settings.seqera.api_url}/workflow/{run.seqera_run_id}"
                respx.get(root, params={"workspaceId": workspace}).mock(
                    return_value=httpx.Response(200, json={"workflow": {"status": "RUNNING"}})
                )
                respx.post(f"{root}/cancel", params={"workspaceId": workspace}).mock(
                    return_value=httpx.Response(200)
                )
            batches[workspace] = respx.post(
                f"{mock_settings.seqera.api_url}/workflow/delete",
                params={"workspaceId": workspace},
                json={"workflowIds": [f"{name}-0", f"{name}-1"]},
            ).mock(return_value=httpx.Response(500 if workspace == failed_workspace else 200))

        result = await bulk_delete_jobs(
            BulkDeleteJobsRequest(runIds=[str(run_id) for run_id, _, _ in runs]),
            owner.id,
            test_db,
            mock_settings,
        )

    assert all(batch.call_count == 1 for batch in batches.values())
    assert set(result.deleted) == {
        str(run_id) for run_id, _, workspace in runs if workspace != failed_workspace
    }
    assert set(result.failed) == {
        str(run_id) for run_id, _, workspace in runs if workspace == failed_workspace
    }
    for run_id, _, workspace in runs:
        assert (test_db.get(WorkflowRun, run_id) is not None) == (workspace == failed_workspace)


async def test_listing_does_not_suppress_missing_backend_error(test_db, mock_settings, backend_run):
    run = backend_run
    run.backend = None
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await list_jobs(
            search=None,
            status_filter=None,
            limit=50,
            offset=0,
            sort_by="submitted",
            sort_order="desc",
            current_user_id=run.owner_user_id,
            db=test_db,
            settings=mock_settings,
        )

    assert exc_info.value.status_code == 500
    assert "has no assigned compute backend" in exc_info.value.detail


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_usage_flag_controls_transfer_planning_and_sync(
    test_db, backend_run, mocker, enabled, force
):
    run = backend_run
    run.backend.service_usage_enabled = enabled
    run.service_usage = 1.0
    test_db.commit()
    prefixes = get_output_spec(run).get_transfer_prefixes(run)
    assert (f"{run.id}/UsageReport.csv" in prefixes) == enabled

    spec = MagicMock(spec=WorkflowResultsSpec)
    spec.get_service_units = AsyncMock(return_value=2.5)
    get_spec = mocker.patch.object(job_utils, "get_output_spec", return_value=spec)
    sync_outputs = mocker.patch.object(job_utils, "sync_workflow_outputs", new_callable=AsyncMock)

    result = await job_utils.sync_service_usage(test_db, run, "Completed", force=force)

    if enabled and force:
        assert result == 2.5
        assert run.service_usage == 2.5
        spec.get_service_units.assert_awaited_once_with(test_db, run)
        sync_outputs.assert_awaited_once()
    else:
        assert result == (1.0 if enabled else None)
        assert run.service_usage == 1.0
        get_spec.assert_not_called()
        sync_outputs.assert_not_awaited()
        spec.get_service_units.assert_not_awaited()
