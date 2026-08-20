"""Tests for DB admin mounting helpers."""

from __future__ import annotations

import os
from collections.abc import Generator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette_admin._types import RequestAction
from starlette_admin.exceptions import ActionFailed

from app.config import get_settings
from app.db.admin import (
    AppUserAdmin,
    DataTransferAdmin,
    NciServiceUnitsField,
    RunOutputAdmin,
    S3ObjectAdmin,
    SbpCreditField,
    WorkflowRunAdmin,
    _claims_has_admin_role,
    _decode_admin_pk,
    _mount_db_debug_api,
    mount_db_admin,
    require_admin_access,
)
from app.db.models.core import AppUser, DataTransfer, RunInput, RunOutput, S3Object, WorkflowRun
from app.routes.dependencies import get_db
from tests.conftest import SettingsNoEnv

DB_ADMIN_REQUIRED_ENV = {
    "AUTH_DOMAIN": "example.auth.test",
    "AUTH_CLIENT_ID": "test-client-id",
    "AUTH_AUDIENCE": "https://example.api.test",
    "DB_ADMIN_AUTH_REDIRECT_URI": "http://localhost:3000/admin/login",
    "DB_ADMIN_SESSION_SECRET": "test-session-secret",
}


def test_is_db_admin_enabled_false_by_default(mocker):
    mocker.patch.dict(os.environ, {}, clear=True)
    # TODO: override get_setttings so it doesn't read from env file
    settings = get_settings()
    assert settings.enable_db_admin is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
def test_is_db_admin_enabled_true_variants(value, mocker):
    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": value})
    settings = SettingsNoEnv()
    assert settings.enable_db_admin is True


def test_mount_db_admin_does_not_mount_when_disabled(mocker, mock_settings):
    app = FastAPI()
    mount_admin = mocker.patch("app.db.admin._mount_starlette_admin")
    mount_debug = mocker.patch("app.db.admin._mount_db_debug_api")

    mock_settings.enable_db_admin = False
    mount_db_admin(app, mock_settings)

    mount_admin.assert_not_called()
    mount_debug.assert_not_called()


def test_mount_db_admin_mounts_both_when_enabled(mocker, mock_settings):
    app = FastAPI()
    mount_admin = mocker.patch("app.db.admin._mount_starlette_admin")
    mount_debug = mocker.patch("app.db.admin._mount_db_debug_api")

    mock_settings.enable_db_admin = True
    mount_db_admin(app, mock_settings)

    mount_admin.assert_called_once_with(app)
    mount_debug.assert_called_once_with(app)


def test_mount_db_admin_registers_debug_router_before_admin_mount(mocker, mock_settings):
    # Starlette Admin mounts a greedy Mount("/admin"). The /admin/debug APIRoutes
    # must be registered BEFORE it, otherwise the Mount shadows them (routes match
    # in registration order) and they 404. (The /admin/api/system-status router is
    # registered in main.py, also before the mount; covered separately.)
    from starlette.routing import Mount

    def route_contains_path(route, path: str) -> bool:
        route_paths = (getattr(route, "path", None), getattr(route, "path_format", None))
        if path in route_paths:
            return True

        nested_routes = list(getattr(route, "routes", []) or [])
        original_router = getattr(route, "original_router", None)
        nested_routes.extend(getattr(original_router, "routes", []) or [])
        return any(route_contains_path(nested_route, path) for nested_route in nested_routes)

    def route_index(path: str, routes) -> int:
        return next(i for i, route in enumerate(routes) if route_contains_path(route, path))

    app = FastAPI()
    mock_settings.enable_db_admin = True
    mount_db_admin(app, mock_settings)

    mount_index = next(
        i for i, r in enumerate(app.router.routes) if isinstance(r, Mount) and r.path == "/admin"
    )

    assert route_index("/admin/debug/s3-objects", app.router.routes) < mount_index


def _admin_field_names(view) -> list[str]:
    """Field entries may be plain strings or field instances (e.g. DateTimeField)."""
    return [getattr(field, "name", field) for field in view.fields]


def test_app_user_admin_includes_credit_column() -> None:
    field_names = _admin_field_names(AppUserAdmin)
    assert "credit" in field_names
    assert "credit_updated_at" in field_names
    assert "credit_updated_by" in field_names


def test_data_transfer_admin_includes_expected_columns() -> None:
    field_names = _admin_field_names(DataTransferAdmin)
    assert "workflow_run" in field_names
    assert "direction" in field_names
    assert "provider" in field_names
    assert "source_location" in field_names
    assert "destination_location" in field_names
    assert "recursive" in field_names
    assert "transfer_id" in field_names
    assert "status" in field_names
    assert "created_at" in field_names


async def test_data_transfer_admin_retry_action_resets_failed_output_transfer(test_db) -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|data-transfer-admin",
        name="Data Transfer Admin",
        email="data-transfer-admin@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="admin-retry-run",
        work_dir="/tmp/admin-retry-run",
    )
    failed_output = DataTransfer(
        workflow_run_id=run.id,
        direction="output",
        provider="globus",
        source_location="/test/output/admin-retry-run/reports/",
        destination_location="s3://bucket/results/admin-retry-run/reports/",
        recursive=True,
        transfer_id="task-stale",
        status="failed",
        error_message="no such file",
    )
    test_db.add_all([user, run, failed_output])
    test_db.commit()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 123),
        }
    )
    request.state.session = test_db
    request.state.action = RequestAction.ROW_ACTION

    view = DataTransferAdmin(DataTransfer)

    request.state.action = RequestAction.DETAIL
    actions = await view.get_all_row_actions(request)
    assert "retry_output_transfer" in {action["name"] for action in actions}

    request.state.action = RequestAction.ROW_ACTION
    message = await view.handle_row_action(
        request,
        str(failed_output.id),
        "retry_output_transfer",
    )

    assert message == "Output transfer reset to pending."
    test_db.refresh(failed_output)
    assert failed_output.status == "pending"
    assert failed_output.transfer_id is None
    assert failed_output.error_message is None


async def test_data_transfer_admin_retry_action_rejects_non_failed_output(test_db) -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|data-transfer-admin-reject",
        name="Data Transfer Admin Reject",
        email="data-transfer-admin-reject@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="admin-retry-reject-run",
        work_dir="/tmp/admin-retry-reject-run",
    )
    failed_input = DataTransfer(
        workflow_run_id=run.id,
        direction="input",
        provider="globus",
        source_location="s3://bucket/input.csv",
        destination_location="/test/input/admin-retry-reject-run/input.csv",
        recursive=False,
        transfer_id="task-input",
        status="failed",
        error_message="input failed",
    )
    test_db.add_all([user, run, failed_input])
    test_db.commit()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 123),
        }
    )
    request.state.session = test_db
    request.state.action = RequestAction.ROW_ACTION

    view = DataTransferAdmin(DataTransfer)
    with pytest.raises(ActionFailed, match="Only failed Globus output transfers"):
        await view.handle_row_action(
            request,
            str(failed_input.id),
            "retry_output_transfer",
        )

    test_db.refresh(failed_input)
    assert failed_input.status == "failed"
    assert failed_input.transfer_id == "task-input"
    assert failed_input.error_message == "input failed"


async def test_data_transfer_admin_batch_retry_action_resets_selected_failed_outputs(
    test_db,
) -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|data-transfer-admin-batch",
        name="Data Transfer Admin Batch",
        email="data-transfer-admin-batch@example.com",
    )
    run = WorkflowRun(
        id=uuid4(),
        owner_user_id=user.id,
        seqera_run_id="admin-batch-retry-run",
        work_dir="/tmp/admin-batch-retry-run",
    )
    failed_output_1 = DataTransfer(
        workflow_run_id=run.id,
        direction="output",
        provider="globus",
        source_location="/test/output/admin-batch-retry-run/reports/",
        destination_location="s3://bucket/results/admin-batch-retry-run/reports/",
        recursive=True,
        transfer_id="task-stale-1",
        status="failed",
        error_message="missing report",
    )
    failed_output_2 = DataTransfer(
        workflow_run_id=run.id,
        direction="output",
        provider="globus",
        source_location="/test/output/admin-batch-retry-run/metrics/",
        destination_location="s3://bucket/results/admin-batch-retry-run/metrics/",
        recursive=True,
        transfer_id="task-stale-2",
        status="failed",
        error_message="missing metrics",
    )
    completed_output = DataTransfer(
        workflow_run_id=run.id,
        direction="output",
        provider="globus",
        source_location="/test/output/admin-batch-retry-run/logs/",
        destination_location="s3://bucket/results/admin-batch-retry-run/logs/",
        recursive=True,
        transfer_id="task-ok",
        status="completed",
        error_message=None,
    )
    failed_input = DataTransfer(
        workflow_run_id=run.id,
        direction="input",
        provider="globus",
        source_location="s3://bucket/input.csv",
        destination_location="/test/input/admin-batch-retry-run/input.csv",
        recursive=False,
        transfer_id="task-input",
        status="failed",
        error_message="input failed",
    )
    test_db.add_all(
        [user, run, failed_output_1, failed_output_2, completed_output, failed_input]
    )
    test_db.commit()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 123),
        }
    )
    request.state.session = test_db
    request.state.action = RequestAction.ACTION

    view = DataTransferAdmin(DataTransfer)

    actions = await view.get_all_actions(request)
    assert "retry_output_transfers" in {action["name"] for action in actions}

    message = await view.handle_action(
        request,
        [
            str(failed_output_1.id),
            str(failed_output_2.id),
            str(completed_output.id),
            str(failed_input.id),
        ],
        "retry_output_transfers",
    )

    assert message == "2 output transfers reset to pending."
    for failed_output in (failed_output_1, failed_output_2):
        test_db.refresh(failed_output)
        assert failed_output.status == "pending"
        assert failed_output.transfer_id is None
        assert failed_output.error_message is None

    test_db.refresh(completed_output)
    assert completed_output.status == "completed"
    assert completed_output.transfer_id == "task-ok"

    test_db.refresh(failed_input)
    assert failed_input.status == "failed"
    assert failed_input.transfer_id == "task-input"
    assert failed_input.error_message == "input failed"


def test_workflow_run_admin_renames_service_usage_and_adds_sbp_credit() -> None:
    field_names = _admin_field_names(WorkflowRunAdmin)
    assert "service_usage" in field_names
    assert "sbp_credit" in field_names

    fields_by_name = {getattr(f, "name", f): f for f in WorkflowRunAdmin.fields}
    assert fields_by_name["service_usage"].label == "NCI Service Units"
    assert fields_by_name["sbp_credit"].label == "SBP Credit"


def test_workflow_run_admin_sbp_credit_excluded_from_forms() -> None:
    # sbp_credit is computed, not stored, so it must not appear on create/edit forms.
    assert "sbp_credit" in WorkflowRunAdmin.exclude_fields_from_create
    assert "sbp_credit" in WorkflowRunAdmin.exclude_fields_from_edit


def test_workflow_run_admin_sbp_credit_not_sortable() -> None:
    # sbp_credit has no backing column, so a click on its list-view column
    # header must not offer to sort by it (that would 500 — starlette-admin
    # would try to build an ORDER BY clause against a nonexistent column).
    assert "sbp_credit" not in WorkflowRunAdmin.sortable_fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12.0, 12.0),
        (12.001, 12.0),
        (12.34, 12.34),
        (12.345, 12.35),
        (0.07, 0.07),
        (0.56, 0.56),
        (2.18, 2.18),
    ],
)
async def test_nci_service_units_field_rounds_for_list_and_detail(raw, expected) -> None:
    field = NciServiceUnitsField("service_usage", label="NCI Service Units")
    for action in (RequestAction.LIST, RequestAction.DETAIL):
        assert await field.serialize_value(None, raw, action) == expected


async def test_nci_service_units_field_keeps_raw_precision_on_forms() -> None:
    field = NciServiceUnitsField("service_usage", label="NCI Service Units")
    for action in (RequestAction.CREATE, RequestAction.EDIT):
        assert await field.serialize_value(None, 12.001, action) == 12.001


async def test_sbp_credit_field_computes_de_novo_design_cost_from_metrics() -> None:
    field = SbpCreditField("sbp_credit", label="SBP Credit")
    run = SimpleNamespace(
        workflow=SimpleNamespace(name="de-novo-design"),
        tool="rfdiffusion",
        metrics=SimpleNamespace(final_design_count=3),
    )
    assert await field.parse_obj(None, run) == 30  # 10 credits/design * 3 designs


async def test_sbp_credit_field_computes_single_prediction_constant_cost() -> None:
    field = SbpCreditField("sbp_credit", label="SBP Credit")
    run = SimpleNamespace(
        workflow=SimpleNamespace(name="single-prediction"),
        tool="colabfold",
        metrics=None,
    )
    assert await field.parse_obj(None, run) == 5


async def test_sbp_credit_field_is_none_for_uncosted_categories_and_missing_data() -> None:
    field = SbpCreditField("sbp_credit", label="SBP Credit")
    bulk_run = SimpleNamespace(
        workflow=SimpleNamespace(name="bulk-prediction"), tool="boltz", metrics=None
    )
    no_workflow_run = SimpleNamespace(workflow=None, tool="boltz", metrics=None)
    no_tool_run = SimpleNamespace(
        workflow=SimpleNamespace(name="single-prediction"), tool=None, metrics=None
    )

    assert await field.parse_obj(None, bulk_run) is None
    assert await field.parse_obj(None, no_workflow_run) is None
    assert await field.parse_obj(None, no_tool_run) is None


def test_app_user_admin_credit_audit_fields_are_read_only_on_forms() -> None:
    # The audit columns are stamped automatically, so they must not be editable
    # via the create/edit forms.
    assert "credit_updated_at" in AppUserAdmin.exclude_fields_from_create
    assert "credit_updated_by" in AppUserAdmin.exclude_fields_from_create
    assert "credit_updated_at" in AppUserAdmin.exclude_fields_from_edit
    assert "credit_updated_by" in AppUserAdmin.exclude_fields_from_edit


async def test_app_user_admin_before_edit_stamps_credit_change(test_db) -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|credit-edit",
        name="Credit Edit",
        email="credit-edit@example.com",
        credit=0,
    )
    test_db.add(user)
    test_db.commit()

    view = AppUserAdmin(AppUser)
    user.credit = 500
    await view.before_edit(None, {}, user)

    assert user.credit == 500
    assert user.credit_updated_by == "admin dashboard"
    assert user.credit_updated_at is not None


async def test_app_user_admin_before_edit_skips_when_credit_unchanged(test_db) -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|credit-noedit",
        name="Credit NoEdit",
        email="credit-noedit@example.com",
        credit=100,
    )
    test_db.add(user)
    test_db.commit()

    view = AppUserAdmin(AppUser)
    # Editing an unrelated field must not stamp the credit audit columns.
    user.name = "Renamed"
    await view.before_edit(None, {}, user)

    assert user.credit_updated_by is None
    assert user.credit_updated_at is None


async def test_app_user_admin_before_create_stamps_when_credit_set() -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|credit-create",
        name="Credit Create",
        email="credit-create@example.com",
        credit=250,
    )

    await AppUserAdmin(AppUser).before_create(None, {}, user)

    assert user.credit_updated_by == "admin dashboard"
    assert user.credit_updated_at is not None


async def test_app_user_admin_before_create_skips_when_no_credit() -> None:
    user = AppUser(
        id=uuid4(),
        auth0_user_id="auth0|credit-create-zero",
        name="Credit Create Zero",
        email="credit-create-zero@example.com",
        credit=0,
    )

    await AppUserAdmin(AppUser).before_create(None, {}, user)

    assert user.credit_updated_by is None
    assert user.credit_updated_at is None


async def test_admin_s3_object_relation_serializes_url_safe_detail_url() -> None:
    async def detail_endpoint(request: Request) -> Response:
        _ = request
        return Response("ok")

    app = Starlette(
        routes=[
            Route("/admin/{identity}/{pk}", detail_endpoint, name="admin:detail"),
            Route("/admin/{identity}/{pk}/edit", detail_endpoint, name="admin:edit"),
        ]
    )
    app.state.ROUTE_NAME = "admin"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 123),
            "router": app.router,
            "app": app,
        }
    )
    request.state.action = RequestAction.API

    s3_view = S3ObjectAdmin(S3Object)
    run_output_view = RunOutputAdmin(RunOutput)
    run_output_view._find_foreign_model = lambda identity: s3_view

    object_key = "Anne_test/ranker/Anne_test_final_design_stats.csv"
    s3_object = S3Object(object_key=object_key, uri=f"s3://bucket/{object_key}")
    run_output = RunOutput(run_id=uuid4(), s3_object_id=object_key)
    run_output.s3_object = s3_object

    serialized = await run_output_view.serialize(
        run_output,
        request,
        RequestAction.API,
        include_relationships=True,
    )

    detail_url = serialized["s3_object"]["_meta"]["detailUrl"]
    encoded_pk = detail_url.rsplit("/", 1)[-1]

    assert object_key not in detail_url
    assert _decode_admin_pk(encoded_pk) == object_key

    row_view_url = s3_view.row_action_1_view(request, object_key)
    row_edit_url = s3_view.row_action_2_edit(request, object_key)

    assert object_key not in row_view_url
    assert object_key not in row_edit_url
    assert _decode_admin_pk(row_view_url.rsplit("/", 1)[-1]) == object_key
    assert _decode_admin_pk(row_edit_url.removesuffix("/edit").rsplit("/", 1)[-1]) == object_key


def test_mount_db_debug_api_endpoints(test_db) -> None:
    # Seed minimal rows so debug endpoints have data to return.
    user_id = uuid4()
    user = AppUser(
        id=user_id,
        auth0_user_id="auth0|admin-debug-test",
        name="Admin Debug Test",
        email="admin-debug@example.com",
    )
    s3_object = S3Object(
        object_key="Anne_test/ranker/Anne_test_final_design_stats.csv",
        uri="s3://bucket/Anne_test/ranker/Anne_test_final_design_stats.csv",
        version_id=None,
        size_bytes=123,
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        workflow_id=None,
        owner_user_id=user_id,
        seqera_run_id="seed-run",
        run_name="seed-run-name",
        binder_name="PDL1",
        work_dir="/tmp/seed-run",
    )
    input_transfer = DataTransfer(
        workflow_run_id=run_id,
        direction="input",
        provider="s3",
        source_location=s3_object.uri,
        destination_location="/tmp/seed-run",
        recursive=False,
    )
    output_transfer = DataTransfer(
        workflow_run_id=run_id,
        direction="output",
        provider="s3",
        source_location="/tmp/seed-run",
        destination_location=s3_object.uri,
        recursive=False,
    )
    run_input = RunInput(
        run_id=run_id, s3_object_id=s3_object.object_key, data_transfer=input_transfer
    )
    run_output = RunOutput(
        run_id=run_id, s3_object_id=s3_object.object_key, data_transfer=output_transfer
    )

    test_db.add(user)
    test_db.add(s3_object)
    test_db.add(run)
    test_db.add(input_transfer)
    test_db.add(output_transfer)
    test_db.add(run_input)
    test_db.add(run_output)
    test_db.commit()

    app = FastAPI()

    def _override_get_db() -> Generator:
        yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    _mount_db_debug_api(app)

    with TestClient(app) as client:
        s3_resp = client.get("/admin/debug/s3-objects?limit=10&offset=0")
        inputs_resp = client.get("/admin/debug/run-inputs?limit=10&offset=0")
        outputs_resp = client.get("/admin/debug/run-outputs?limit=10&offset=0")

    assert s3_resp.status_code == 200
    assert inputs_resp.status_code == 200
    assert outputs_resp.status_code == 200

    s3_json = s3_resp.json()
    inputs_json = inputs_resp.json()
    outputs_json = outputs_resp.json()

    assert s3_json["total"] >= 1
    assert any(item["object_key"] == s3_object.object_key for item in s3_json["items"])
    assert inputs_json["total"] >= 1
    assert any(item["run_id"] == str(run_id) for item in inputs_json["items"])
    assert outputs_json["total"] >= 1
    assert any(item["run_id"] == str(run_id) for item in outputs_json["items"])


def test_claims_has_admin_role_from_direct_claim(mock_settings) -> None:
    required_role = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mock_settings.auth.required_role = required_role
    mock_settings.admin.roles_claim = roles_claim_name
    claims = {roles_claim_name: [required_role]}
    assert _claims_has_admin_role(claims, mock_settings) is True


def test_claims_has_admin_role_from_roles_claim_list(mocker, mock_settings) -> None:
    required = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mock_settings.auth.required_role = required
    mock_settings.admin.roles_claim = roles_claim_name
    claims = {roles_claim_name: [required, "biocommons/role/sbp/user"]}
    assert _claims_has_admin_role(claims, mock_settings) is True


def test_claims_has_admin_role_missing(mocker, mock_settings) -> None:
    required = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mock_settings.auth.required_role = required
    mock_settings.admin.roles_claim = roles_claim_name
    claims = {roles_claim_name: ["something/else"]}
    assert _claims_has_admin_role(claims, mock_settings) is False
