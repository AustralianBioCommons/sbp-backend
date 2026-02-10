"""Tests for results routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


def test_get_result_setting_params_route(client: TestClient):
    with patch(
        "app.routes.results.get_owned_run",
        return_value=SimpleNamespace(
            submitted_form_data={
                "id": "s1",
                "binder_name": "PDL1",
                "number_of_final_designs": 100,
            },
            sample_id="s1",
            binder_name="PDL1",
            metrics=SimpleNamespace(final_design_count=100),
        ),
    ):
        response = client.get("/api/results/wf-1/settingParams")

    assert response.status_code == 200
    assert response.json() == {
        "runId": "wf-1",
        "settingParams": {
            "id": "s1",
            "binder_name": "PDL1",
            "number_of_final_designs": 100,
        },
    }
