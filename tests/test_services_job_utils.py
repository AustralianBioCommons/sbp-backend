"""Coverage tests for job utility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from app.services import job_utils


class _Result:
    def __init__(self, all_value=None, scalar_value=None):
        self._all = all_value or []
        self._scalar = scalar_value

    def all(self):
        return self._all

    def scalar_one_or_none(self):
        return self._scalar


class _DB:
    def __init__(self, all_rows=None, scalar=None):
        self._all_rows = all_rows or []
        self._scalar = scalar
        self.added = None
        self.committed = False

    def execute(self, *_args, **_kwargs):
        return _Result(all_value=self._all_rows, scalar_value=self._scalar)

    def add(self, obj):
        self.added = obj

    def commit(self):
        self.committed = True


def test_coerce_and_extract_helpers():
    payload = {"workflow": {"status": "RUNNING"}}
    assert job_utils.coerce_workflow_payload(payload) == payload["workflow"]
    assert job_utils.extract_pipeline_status(payload) == "RUNNING"


def test_parse_submit_datetime_invalid_returns_none():
    assert job_utils.parse_submit_datetime({"workflow": {"submit": "bad"}}) is None


def test_get_owned_run_ids_and_scores_and_workflow_type():
    uid = UUID("11111111-1111-1111-1111-111111111111")

    db_ids = _DB(all_rows=[("wf-1",), ("wf-2",)])
    assert job_utils.get_owned_run_ids(db_ids, uid) == {"wf-1", "wf-2"}

    db_scores = _DB(all_rows=[("wf-1", 0.9123), ("wf-2", None)])
    assert job_utils.get_score_by_seqera_run_id(db_scores, uid) == {"wf-1": 0.912}

    db_types = _DB(all_rows=[("wf-1", "BindCraft")])
    assert job_utils.get_workflow_type_by_seqera_run_id(db_types, uid) == {"wf-1": "BindCraft"}


@pytest.mark.asyncio
async def test_ensure_completed_run_score_branches():
    run = SimpleNamespace(id="rid", seqera_run_id="wf-1")

    # non-completed status
    assert await job_utils.ensure_completed_run_score(_DB(), run, "Failed") is None

    # existing score path
    db_existing = _DB(scalar=SimpleNamespace(max_score=0.9))
    assert await job_utils.ensure_completed_run_score(db_existing, run, "Completed") == 0.9

    # calculate + add path
    db_new = _DB(scalar=None)
    with patch(
        "app.services.job_utils.calculate_csv_column_max", new_callable=AsyncMock, return_value=1.23
    ):
        score = await job_utils.ensure_completed_run_score(db_new, run, "Completed")
    assert score == 1.0
    assert db_new.added is not None
    assert db_new.committed is True

    # calculate failure path
    db_fail = _DB(scalar=None)
    with patch(
        "app.services.job_utils.calculate_csv_column_max",
        new_callable=AsyncMock,
        side_effect=ValueError("bad"),
    ):
        assert await job_utils.ensure_completed_run_score(db_fail, run, "Completed") is None
