"""Tests for DB admin mounting helpers."""

from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.admin import _is_db_admin_enabled, _mount_db_debug_api, mount_db_admin
from app.db.models.core import AppUser, RunInput, RunOutput, S3Object, WorkflowRun
from app.routes.dependencies import get_db


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

    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "true"})
    mount_db_admin(app)

    mount_admin.assert_called_once_with(app)
    mount_debug.assert_called_once_with(app)


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
