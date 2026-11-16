"""Workflow-specific eligibility for compute backend selection."""

import pytest
from fastapi import HTTPException

from app.schemas.backend import BackendSelectionMethod, ComputeBackendName
from app.services.compute_backends import (
    get_enabled_backends,
    get_next_backend_for_launch,
    select_next_backend,
)
from tests.datagen import (
    BackendSelectionSettingsFactory,
    BackendWorkflowSettingsFactory,
    ComputeBackendFactory,
    WorkflowFactory,
)


@pytest.fixture
def selection_context(persistent_models):
    workflow = WorkflowFactory.create_sync()
    gadi = ComputeBackendFactory.create_sync(name=ComputeBackendName.GADI, enabled=True)
    setonix = ComputeBackendFactory.create_sync(name=ComputeBackendName.SETONIX, enabled=True)
    return workflow, gadi, setonix


@pytest.mark.parametrize("override_enabled", [None, True, False])
def test_override_controls_only_its_backend(test_db, selection_context, override_enabled):
    workflow, gadi, setonix = selection_context
    if override_enabled is not None:
        BackendWorkflowSettingsFactory.create_sync(
            workflow=workflow,
            compute_backend=gadi,
            enabled=override_enabled,
        )
    backends = get_enabled_backends(test_db, workflow)
    expected = [setonix] if override_enabled is False else [gadi, setonix]
    assert list(backends) == expected


def test_disabled_override_for_another_workflow_is_ignored(test_db, selection_context):
    workflow, gadi, setonix = selection_context
    BackendWorkflowSettingsFactory.create_sync(
        workflow=WorkflowFactory.create_sync(),
        compute_backend=gadi,
        enabled=False,
    )
    assert list(get_enabled_backends(test_db, workflow)) == [gadi, setonix]


def test_workflow_override_cannot_enable_globally_disabled_backend(test_db, selection_context):
    workflow, gadi, setonix = selection_context
    gadi.enabled = False
    BackendWorkflowSettingsFactory.create_sync(
        workflow=workflow,
        compute_backend=gadi,
        enabled=True,
    )
    assert list(get_enabled_backends(test_db, workflow)) == [setonix]


@pytest.mark.parametrize("method", list(BackendSelectionMethod))
def test_selection_skips_backend_with_disabled_workflow(test_db, selection_context, method):
    workflow, gadi, setonix = selection_context
    BackendWorkflowSettingsFactory.create_sync(
        workflow=workflow,
        compute_backend=gadi,
        enabled=False,
    )
    settings = BackendSelectionSettingsFactory.create_sync(
        selection_method=method,
        last_selected_id=gadi.id,
    )
    assert select_next_backend(test_db, settings, workflow) is setonix
    if method == BackendSelectionMethod.CYCLE:
        assert settings.last_selected_id == setonix.id
        # A sole eligible backend remains selectable on the following cycle.
        assert select_next_backend(test_db, settings, workflow) is setonix


def test_cycle_uses_remaining_backends_when_next_backend_is_disabled(test_db, selection_context):
    workflow, gadi, setonix = selection_context
    BackendWorkflowSettingsFactory.create_sync(
        workflow=workflow,
        compute_backend=setonix,
        enabled=False,
    )
    settings = BackendSelectionSettingsFactory.create_sync(
        selection_method=BackendSelectionMethod.CYCLE,
        last_selected_id=gadi.id,
    )
    assert select_next_backend(test_db, settings, workflow) is gadi


@pytest.mark.parametrize("method", list(BackendSelectionMethod))
def test_no_eligible_backend_returns_503(test_db, selection_context, method):
    workflow, gadi, setonix = selection_context
    for backend in (gadi, setonix):
        BackendWorkflowSettingsFactory.create_sync(
            workflow=workflow,
            compute_backend=backend,
            enabled=False,
        )
    settings = BackendSelectionSettingsFactory.create_sync(
        selection_method=method,
        last_selected_id=gadi.id,
    )
    with pytest.raises(HTTPException) as exc:
        get_next_backend_for_launch(test_db, settings, workflow)
    assert exc.value.status_code == 503
    assert settings.last_selected_id == gadi.id
