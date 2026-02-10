"""Coverage tests for job utility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.db.models.core import AppUser, RunMetric, Workflow, WorkflowRun
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


def test_get_owned_run_ids_returns_only_current_user_runs(test_db):
    """Test that get_owned_run_ids returns only runs owned by the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add(user1)
    test_db.add(user2)
    test_db.commit()

    # Create runs for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    run2_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-2",
        work_dir="workdir-1002",
    )

    # Create runs for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    run2_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-2",
        work_dir="workdir-2002",
    )

    test_db.add_all([run1_user1, run2_user1, run1_user2, run2_user2])
    test_db.commit()

    # Get run IDs for user1 - should only return user1's runs
    user1_runs = job_utils.get_owned_run_ids(test_db, user1.id)
    assert user1_runs == {"run-user1-1", "run-user1-2"}
    assert "run-user2-1" not in user1_runs
    assert "run-user2-2" not in user1_runs

    # Get run IDs for user2 - should only return user2's runs
    user2_runs = job_utils.get_owned_run_ids(test_db, user2.id)
    assert user2_runs == {"run-user2-1", "run-user2-2"}
    assert "run-user1-1" not in user2_runs
    assert "run-user1-2" not in user2_runs


def test_get_score_by_seqera_run_id_returns_only_current_user_runs(test_db):
    """Test that get_score_by_seqera_run_id returns only scores for the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add_all([user1, user2])
    test_db.commit()

    # Create runs with metrics for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    run2_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        seqera_run_id="run-user1-2",
        work_dir="workdir-1002",
    )
    test_db.add_all([run1_user1, run2_user1])
    test_db.commit()

    # Add metrics
    metric1 = RunMetric(run_id=run1_user1.id, max_score=0.9123)
    metric2 = RunMetric(run_id=run2_user1.id, max_score=None)
    test_db.add_all([metric1, metric2])

    # Create runs with metrics for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    test_db.add(run1_user2)
    test_db.commit()

    metric3 = RunMetric(run_id=run1_user2.id, max_score=0.5555)
    test_db.add(metric3)
    test_db.commit()

    # Get scores for user1
    user1_scores = job_utils.get_score_by_seqera_run_id(test_db, user1.id)
    assert user1_scores == {"run-user1-1": 0.91}  # Numeric(8, 2) precision
    assert "run-user2-1" not in user1_scores

    # Get scores for user2
    user2_scores = job_utils.get_score_by_seqera_run_id(test_db, user2.id)
    assert user2_scores == {"run-user2-1": 0.56}  # Numeric(8, 2) precision
    assert "run-user1-1" not in user2_scores


def test_get_workflow_type_by_seqera_run_id_returns_only_current_user_runs(test_db):
    """Test that get_workflow_type_by_seqera_run_id returns only workflow types for the specified user."""
    # Create two users
    user1 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user1",
        name="User One",
        email="user1@example.com",
    )
    user2 = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|user2",
        name="User Two",
        email="user2@example.com",
    )
    test_db.add_all([user1, user2])
    test_db.commit()

    # Create workflows
    workflow1 = Workflow(
        id=uuid4(),
        name="BindCraft",
        description="Binding workflow",
    )
    workflow2 = Workflow(
        id=uuid4(),
        name="OtherWorkflow",
        description="Other workflow",
    )
    test_db.add_all([workflow1, workflow2])
    test_db.commit()

    # Create runs for user1
    run1_user1 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user1.id,
        workflow_id=workflow1.id,
        seqera_run_id="run-user1-1",
        work_dir="workdir-1001",
    )
    test_db.add(run1_user1)

    # Create runs for user2
    run1_user2 = WorkflowRun(
        id=uuid4(),
        owner_user_id=user2.id,
        workflow_id=workflow2.id,
        seqera_run_id="run-user2-1",
        work_dir="workdir-2001",
    )
    test_db.add(run1_user2)
    test_db.commit()

    # Get workflow types for user1
    user1_types = job_utils.get_workflow_type_by_seqera_run_id(test_db, user1.id)
    assert user1_types == {"run-user1-1": "BindCraft"}
    assert "run-user2-1" not in user1_types

    # Get workflow types for user2
    user2_types = job_utils.get_workflow_type_by_seqera_run_id(test_db, user2.id)
    assert user2_types == {"run-user2-1": "OtherWorkflow"}
    assert "run-user1-1" not in user2_types


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
