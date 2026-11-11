import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ComputeBackend, WorkflowRun
from app.schemas.backend import ComputeBackendName
from tests.datagen import AppUserFactory, ComputeBackendFactory, WorkflowRunFactory


def test_get_backend_by_name(test_db, persistent_models):
    backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI)

    assert ComputeBackend.get_by_name(test_db, ComputeBackendName.GADI) is backend
    assert ComputeBackend.get_by_name(test_db, ComputeBackendName.SETONIX) is None


def test_factory_builds_without_backend():
    run = WorkflowRunFactory.build()

    assert run.backend is None
    assert run.backend_id is None


@pytest.mark.parametrize("backend_values", [{}, {"backend": None}, {"backend_id": None}])
def test_runs_can_be_persisted_without_backend(test_db, persistent_models, backend_values):
    owner = AppUserFactory.create_sync()
    direct_run = WorkflowRun(owner=owner, work_dir="/test/work", **backend_values)
    factory_run = WorkflowRunFactory.build(owner=owner, **backend_values)
    test_db.add_all([direct_run, factory_run])

    test_db.commit()

    for run in (direct_run, factory_run):
        test_db.refresh(run)
        assert run.backend_id is None
        assert run.backend is None
    assert test_db.scalar(select(func.count()).select_from(ComputeBackend)) == 0


@pytest.mark.parametrize("field_name", ["backend", "backend_id"])
def test_explicit_backend_is_preserved(test_db, persistent_models, field_name):
    backend = ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX)
    owner = AppUserFactory.create_sync()
    value = backend if field_name == "backend" else backend.id

    run = WorkflowRunFactory.create_sync(owner=owner, **{field_name: value})

    assert run.backend_id == backend.id
    assert run.backend is backend
    assert test_db.scalar(select(func.count()).select_from(ComputeBackend)) == 1


def test_backend_can_be_referenced_across_sessions(
    test_engine, test_db, persistent_models, default_backend
):
    owner = AppUserFactory.create_sync()
    first_run = WorkflowRunFactory.create_sync(owner=owner, backend=default_backend)

    with Session(test_engine) as session:
        second_run = WorkflowRun(
            owner_user_id=owner.id, work_dir="/test/other-work", backend_id=first_run.backend_id
        )
        session.add(second_run)
        session.flush()

        assert second_run.backend_id == first_run.backend_id
        assert session.scalar(select(func.count()).select_from(ComputeBackend)) == 1
