"""Tests for Seqera workflow parsers."""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.seqera_parsers import (
    extract_workflow_type,
    parse_workflow_list_payload,
)


def test_extract_workflow_type_bindcraft():
    """Test extraction of BindCraft workflow type."""
    data = {"projectName": "my-bindcraft-project"}
    assert extract_workflow_type(data) == "BindCraft"

    data = {"pipeline": "bindcraft-pipeline"}
    assert extract_workflow_type(data) == "BindCraft"


def test_extract_workflow_type_denovo():
    """Test extraction of De novo design workflow type."""
    data = {"projectName": "denovo-pipeline"}
    assert extract_workflow_type(data) == "De novo design"

    data = {"pipeline": "de-novo-design"}
    assert extract_workflow_type(data) == "De novo design"


def test_extract_workflow_type_other():
    """Test extraction of other workflow types."""
    data = {"projectName": "custom-pipeline"}
    assert extract_workflow_type(data) == "custom-pipeline"

    data = {"pipeline": "test-workflow"}
    assert extract_workflow_type(data) == "test-workflow"


def test_extract_workflow_type_empty():
    """Test extraction with missing data."""
    assert extract_workflow_type({}) is None
    assert extract_workflow_type({"projectName": ""}) is None


def test_parse_workflow_list_payload_dict_format():
    """Test parsing workflow list from dict format."""
    data = {
        "workflows": [
            {
                "id": "wf-1",
                "runName": "Test Run",
                "projectName": "bindcraft",
                "status": "SUCCEEDED",
                "submit": "2026-02-01T10:00:00Z",
            }
        ],
        "totalSize": 1,
    }

    items, total = parse_workflow_list_payload(data)

    assert total == 1
    assert len(items) == 1
    assert items[0].workflow_id == "wf-1"
    assert items[0].run_name == "Test Run"
    assert items[0].workflow_type == "BindCraft"
    assert items[0].ui_status == "Completed"
    assert items[0].pipeline_status == "SUCCEEDED"
    assert items[0].submitted_at == datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_workflow_list_payload_list_format():
    """Test parsing workflow list from list format."""
    data = [
        {
            "id": "wf-1",
            "runName": "Test Run",
            "status": "RUNNING",
        }
    ]

    items, total = parse_workflow_list_payload(data)

    assert total == 1
    assert len(items) == 1
    assert items[0].workflow_id == "wf-1"
    assert items[0].ui_status == "In progress"


def test_parse_workflow_list_payload_with_nested_workflow():
    """Test parsing with nested workflow structure."""
    data = {
        "items": [
            {
                "workflow": {
                    "id": "wf-2",
                    "runName": "Nested Run",
                    "status": "FAILED",
                }
            }
        ],
        "total": 1,
    }

    items, total = parse_workflow_list_payload(data)

    assert total == 1
    assert len(items) == 1
    assert items[0].workflow_id == "wf-2"
    assert items[0].run_name == "Nested Run"
    assert items[0].ui_status == "Failed"


def test_parse_workflow_list_payload_with_status_filter():
    """Test filtering by status."""
    data = {
        "workflows": [
            {"id": "wf-1", "status": "SUCCEEDED"},
            {"id": "wf-2", "status": "FAILED"},
            {"id": "wf-3", "status": "RUNNING"},
        ]
    }

    items, total = parse_workflow_list_payload(data, status_filter=["Completed", "Failed"])

    assert total == 3  # Total before filtering
    assert len(items) == 2
    assert items[0].workflow_id == "wf-1"
    assert items[1].workflow_id == "wf-2"


def test_parse_workflow_list_payload_with_search():
    """Test filtering by search query."""
    data = {
        "workflows": [
            {"id": "wf-1", "runName": "Matching Job", "projectName": "test"},
            {"id": "wf-2", "runName": "Other Job", "projectName": "bindcraft"},
            {"id": "wf-3", "runName": "Another", "projectName": "test"},
        ]
    }

    items, total = parse_workflow_list_payload(data, search_query="matching")

    assert len(items) == 1
    assert items[0].workflow_id == "wf-1"

    items, total = parse_workflow_list_payload(data, search_query="bindcraft")
    assert len(items) == 1
    assert items[0].workflow_id == "wf-2"


def test_parse_workflow_list_payload_with_date_created():
    """Test parsing with dateCreated field."""
    data = {
        "workflows": [
            {
                "id": "wf-1",
                "dateCreated": "2026-02-05T15:30:00Z",
                "status": "RUNNING",
            }
        ]
    }

    items, total = parse_workflow_list_payload(data)

    assert len(items) == 1
    assert items[0].submitted_at == datetime(2026, 2, 5, 15, 30, 0, tzinfo=timezone.utc)


def test_parse_workflow_list_payload_invalid_date():
    """Test handling of invalid date formats."""
    data = {
        "workflows": [
            {
                "id": "wf-1",
                "submit": "invalid-date",
                "status": "RUNNING",
            }
        ]
    }

    items, total = parse_workflow_list_payload(data)

    assert len(items) == 1
    assert items[0].submitted_at is None


def test_parse_workflow_list_payload_empty():
    """Test parsing empty data."""
    items, total = parse_workflow_list_payload({})
    assert len(items) == 0
    assert total == 0

    items, total = parse_workflow_list_payload([])
    assert len(items) == 0
    assert total == 0

    items, total = parse_workflow_list_payload(None)
    assert len(items) == 0
    assert total == 0


def test_parse_workflow_list_payload_missing_fields():
    """Test parsing with missing optional fields."""
    data = {
        "workflows": [
            {
                "id": "wf-1",
                "status": "SUCCEEDED",
                # Missing runName, projectName, submit
            }
        ]
    }

    items, total = parse_workflow_list_payload(data)

    assert len(items) == 1
    assert items[0].workflow_id == "wf-1"
    assert items[0].run_name is None
    assert items[0].workflow_type is None
    assert items[0].submitted_at is None
