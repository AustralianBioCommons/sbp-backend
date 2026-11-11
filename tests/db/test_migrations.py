"""Tests for database migrations."""

import re
from io import StringIO
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


def test_backend_models_downgrade_removes_created_enum_types():
    migration = load_python_file(
        str(Path(__file__).resolve().parents[2] / "alembic" / "versions"),
        "20261110_140917_backend_models_4077dfccd11c.py",
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )

    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()

    sql = output.getvalue()
    created_types = set(re.findall(r"^CREATE TYPE (\w+) AS ENUM", sql, re.MULTILINE))
    dropped_types = set(re.findall(r"^DROP TYPE (\w+);", sql, re.MULTILINE))
    assert created_types
    assert dropped_types == created_types
    assert sql.index("DROP TYPE") > sql.rindex("DROP TABLE")
