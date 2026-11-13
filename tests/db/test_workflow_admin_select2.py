"""Regression coverage for Workflow relationship picker responses."""

from xml.etree.ElementTree import fromstring

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette_admin.contrib.sqla import Admin

from app.db.admin import WorkflowAdmin
from app.db.models.core import Workflow
from tests.datagen import WorkflowFactory


@pytest.mark.parametrize(
    ("name", "tool", "label"),
    [
        ("protein-fold", "colabfold", "protein-fold: colabfold"),
        ("protein-fold", None, "protein-fold"),
        ("protein-fold <example>", "tool & more", "protein-fold <example>: tool & more"),
    ],
)
def test_workflow_picker_returns_renderable_labels(
    test_db, test_engine, persistent_models, name, tool, label
):
    workflow = WorkflowFactory.create_sync(name=name, tool=tool)
    test_db.commit()
    workflow_id = str(workflow.id)

    app = FastAPI()
    admin = Admin(test_engine)
    admin.add_view(WorkflowAdmin(Workflow))
    admin.mount_to(app)

    with TestClient(app) as client:
        for params in (
            {"select2": "true", "order_by": "id asc"},
            {"select2": "true", "order_by": "id asc", "where": "protein-fold"},
            {"select2": "true", "pks": workflow_id},
        ):
            response = client.get("/admin/api/workflow", params=params)
            assert response.status_code == 200
            items = response.json()["items"]
            assert len(items) == 1
            assert items[0]["id"] == workflow_id
            for key in ("result", "selection"):
                element = fromstring(items[0]["_meta"]["select2"][key])
                assert element.tag == "span"
                assert element.text == label
                assert len(element) == 0
