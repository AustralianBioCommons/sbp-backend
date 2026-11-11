"""Tests for data backfills in database migrations."""

from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import load_python_file

from app.db.models import WorkflowRun
from app.schemas.backend import ComputeBackendName
from tests.datagen import (
    AppUserFactory,
    ComputeBackendFactory,
    WorkflowFactory,
    WorkflowRunFactory,
)


def test_run_backend_link_upgrade_backfills_existing_runs(
    test_db, persistent_models, mocker, monkeypatch
):
    ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    gadi = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)
    owner = AppUserFactory.create_sync()
    workflow = WorkflowFactory.create_sync()
    runs = WorkflowRunFactory.create_batch_sync(3, owner=owner, workflow=workflow, backend_id=None)
    migration = load_python_file(
        str(Path(__file__).resolve().parents[2] / "alembic" / "versions"),
        "20261111_142823_run_backend_link_2dac78412d99.py",
    )

    connection = test_db.connection()
    # Teach SQLite reflection about PostgreSQL UUID columns.
    monkeypatch.setitem(connection.dialect.ischema_names, "UUID", sa.UUID)
    # The fixture already has the column and foreign key.
    mocker.patch("alembic.op.add_column")
    mocker.patch("alembic.op.create_foreign_key")

    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()

    rows = test_db.execute(sa.select(WorkflowRun.id, WorkflowRun.backend_id)).all()
    assert {tuple(row) for row in rows} == {(run.id, gadi.id) for run in runs}
