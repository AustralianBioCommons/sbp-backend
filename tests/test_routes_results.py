"""Tests for results routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.models.core import AppUser, RunMetric, WorkflowRun
from app.routes.workflow.results import get_result_logs, get_result_setting_params


@pytest.mark.asyncio
async def test_get_result_setting_params_uses_stored_form_data(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user",
        name="Results User",
        email="results@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-1",
        submitted_form_data={
            "id": "s1",
            "binder_name": "PDL1",
            "number_of_final_designs": 100,
        },
        sample_id="s1",
        binder_name="PDL1",
        work_dir="/tmp/wf-1",
    )
    test_db.add_all([user, run])
    test_db.add(RunMetric(run_id=run.id, final_design_count=100))
    test_db.commit()

    result = await get_result_setting_params("wf-1", user.id, test_db)

    assert result.runId == "wf-1"
    assert result.settingParams == {
        "id": "s1",
        "binder_name": "PDL1",
        "number_of_final_designs": 100,
    }


@pytest.mark.asyncio
async def test_get_result_setting_params_falls_back_to_local_fields(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-2",
        name="Results User 2",
        email="results2@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-2",
        submitted_form_data=None,
        sample_id="s2",
        binder_name="PDL2",
        work_dir="/tmp/wf-2",
    )
    test_db.add_all([user, run])
    test_db.add(RunMetric(run_id=run.id, final_design_count=25))
    test_db.commit()

    result = await get_result_setting_params("wf-2", user.id, test_db)

    assert result.runId == "wf-2"
    assert result.settingParams == {
        "id": "s2",
        "binder_name": "PDL2",
        "number_of_final_designs": 25,
        "_source": "fallback_local",
        "_warning": "submitted_form_data_missing",
    }


@pytest.mark.asyncio
async def test_get_result_logs_returns_formatted_entries(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-3",
        name="Results User 3",
        email="results3@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="wf-logs-1",
        work_dir="/tmp/wf-logs-1",
    )
    test_db.add_all([user, run])
    test_db.commit()

    payload = {
        "log": {
            "truncated": False,
            "pending": False,
            "message": "Logs retrieved",
            "rewindToken": "rewind-1",
            "forwardToken": "forward-1",
            "downloads": [{"label": "raw", "url": "https://example.test/logs.txt"}],
            "entries": [
                "2026-03-10T10:00:00Z INFO Starting workflow",
                "  \u001b[0;34mworkDir                   : \u001b[0;32m/scratch/yz52/sbp/workdir\u001b[0m",
            ],
        }
    }

    with patch(
        "app.routes.workflow.results.get_workflow_logs_raw",
        new=AsyncMock(return_value=payload),
    ):
        result = await get_result_logs("wf-logs-1", user.id, test_db)

    assert result.runId == "wf-logs-1"
    assert result.entries == payload["log"]["entries"]
    assert result.message == "Logs retrieved"
    assert len(result.formattedEntries) == 2
    assert result.formattedEntries[0].timestamp == "2026-03-10T10:00:00Z"
    assert result.formattedEntries[0].level == "INFO"
    assert result.formattedEntries[0].message == "INFO Starting workflow"
    assert (
        result.formattedEntries[1].message
        == "workDir                   : /scratch/yz52/sbp/workdir"
    )


@pytest.mark.asyncio
async def test_get_result_logs_returns_404_for_missing_owned_run(test_db):
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|results-user-4",
        name="Results User 4",
        email="results4@example.com",
    )
    test_db.add(user)
    test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_result_logs("wf-logs-missing", user.id, test_db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"
