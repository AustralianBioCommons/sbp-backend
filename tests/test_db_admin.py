"""Tests for DB admin mounting helpers."""

from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette_admin._types import RequestAction

from app.db.admin import (
    AppUserAdmin,
    RunOutputAdmin,
    S3ObjectAdmin,
    _claims_has_admin_role,
    _decode_admin_pk,
    _is_db_admin_enabled,
    _mount_db_debug_api,
    mount_db_admin,
    require_admin_access,
)
from app.db.models.core import AppUser, RunInput, RunOutput, S3Object, WorkflowRun
from app.routes.dependencies import get_db

DB_ADMIN_REQUIRED_ENV = {
    "AUTH_DOMAIN": "example.auth.test",
    "AUTH_CLIENT_ID": "test-client-id",
    "AUTH_AUDIENCE": "https://example.api.test",
    "DB_ADMIN_AUTH_REDIRECT_URI": "http://localhost:3000/admin/login",
    "DB_ADMIN_SESSION_SECRET": "test-session-secret",
}


def test_is_db_admin_enabled_false_by_default(mocker):
    mocker.patch.dict(os.environ, {}, clear=True)
    assert _is_db_admin_enabled() is False


def test_is_db_admin_enabled_true_variants(mocker):
    for value in ("1", "true", "yes", "TRUE"):
        mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": value})
        assert _is_db_admin_enabled() is True


def test_mount_db_admin_does_not_mount_when_disabled(mocker):
    app = FastAPI()
    mount_admin = mocker.patch("app.db.admin._mount_starlette_admin")
    mount_debug = mocker.patch("app.db.admin._mount_db_debug_api")

    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "false"})
    mount_db_admin(app)

    mount_admin.assert_not_called()
    mount_debug.assert_not_called()


def test_mount_db_admin_mounts_both_when_enabled(mocker):
    app = FastAPI()
    mount_admin = mocker.patch("app.db.admin._mount_starlette_admin")
    mount_debug = mocker.patch("app.db.admin._mount_db_debug_api")

    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "true", **DB_ADMIN_REQUIRED_ENV})
    mount_db_admin(app)

    mount_admin.assert_called_once_with(app)
    mount_debug.assert_called_once_with(app)


def test_mount_db_admin_registers_api_routers_before_admin_mount(mocker):
    # Starlette Admin mounts a greedy Mount("/admin"). The /admin/api and
    # /admin/debug APIRoutes must be registered BEFORE it, otherwise the Mount
    # shadows them (routes match in registration order) and they 404.
    from starlette.routing import Mount

    app = FastAPI()
    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "true", **DB_ADMIN_REQUIRED_ENV})
    mount_db_admin(app)

    paths = [getattr(r, "path", getattr(r, "path_format", "")) for r in app.router.routes]
    mount_index = next(
        i for i, r in enumerate(app.router.routes) if isinstance(r, Mount) and r.path == "/admin"
    )
    system_status_index = paths.index("/admin/api/system-status")

    assert system_status_index < mount_index
    assert paths.index("/admin/debug/s3-objects") < mount_index


def test_mount_db_admin_raises_when_enabled_with_missing_env(mocker):
    app = FastAPI()
    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "true"}, clear=True)

    with pytest.raises(RuntimeError, match="required DB admin env vars are missing"):
        mount_db_admin(app)


def _admin_field_names(view) -> list[str]:
    """Field entries may be plain strings or field instances (e.g. DateTimeField)."""
    return [getattr(field, "name", field) for field in view.fields]


def test_app_user_admin_includes_credit_column() -> None:
    field_names = _admin_field_names(AppUserAdmin)
    assert "credit" in field_names
    assert "credit_updated_at" in field_names
    assert "credit_updated_by" in field_names


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
        seqera_dataset_id=None,
        seqera_run_id="seed-run",
        run_name="seed-run-name",
        binder_name="PDL1",
        work_dir="/tmp/seed-run",
    )
    run_input = RunInput(run_id=run_id, s3_object_id=s3_object.object_key)
    run_output = RunOutput(run_id=run_id, s3_object_id=s3_object.object_key)

    test_db.add(user)
    test_db.add(s3_object)
    test_db.add(run)
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


def test_claims_has_admin_role_from_direct_claim(mocker) -> None:
    required_role = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mocker.patch.dict(
        os.environ,
        {
            "DB_ADMIN_REQUIRED_ROLE": required_role,
            "DB_ADMIN_ROLES_CLAIM": roles_claim_name,
        },
    )
    claims = {roles_claim_name: [required_role]}
    assert _claims_has_admin_role(claims) is True


def test_claims_has_admin_role_from_roles_claim_list(mocker) -> None:
    required = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mocker.patch.dict(
        os.environ,
        {
            "DB_ADMIN_REQUIRED_ROLE": required,
            "DB_ADMIN_ROLES_CLAIM": roles_claim_name,
        },
    )
    claims = {roles_claim_name: [required, "biocommons/role/sbp/user"]}
    assert _claims_has_admin_role(claims) is True


def test_claims_has_admin_role_missing(mocker) -> None:
    required = "biocommons/role/sbp/admin"
    roles_claim_name = "https://biocommons.org.au/roles"
    mocker.patch.dict(
        os.environ,
        {
            "DB_ADMIN_REQUIRED_ROLE": required,
            "DB_ADMIN_ROLES_CLAIM": roles_claim_name,
        },
    )
    claims = {roles_claim_name: ["something/else"]}
    assert _claims_has_admin_role(claims) is False
