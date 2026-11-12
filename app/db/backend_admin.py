from html import escape
from typing import Any

from starlette.requests import Request
from starlette_admin import HasOne
from starlette_admin.contrib.sqla import ModelView

from .models.backends import ComputeBackend
from .models.core import Workflow

# ModelView type annotations for fields etc. are
#   not great, reject str. Ignore for now
# mypy: ignore-errors


class ComputeBackendAdmin(ModelView):
    fields = [
        "name",
        "label",
        "enabled",
        "seqera_workspace_id",
        "seqera_compute_env_id",
        "env_prefix",
        "base_work_dir",
        "project_id",
        "repository_transfer",
        "data_transfer",
        "max_workflows",
        "service_usage_enabled",
        "created_at",
        "updated_at",
        "id",
    ]
    searchable_fields = ["name", "label"]
    # Relationship pickers request results ordered by the primary key.
    sortable_fields = [
        "id",
        "name",
        "created_at",
    ]
    fields_default_sort = [("name", False)]

    async def repr(self, obj: Any, request: Request) -> str:
        return f"ComputeBackend('{obj.name}')"

    async def select2_result(self, obj: Any, request: Request) -> str:
        return f"<span>{escape(obj.name)}</span>"


class BackendWorkflowSettingsAdmin(ModelView):
    fields = [
        "id",
        HasOne("compute_backend", identity="compute-backend"),
        HasOne("workflow", identity="workflow"),
        "enabled",
        "config_path",
        "ref_database_dir",
    ]
    sortable_fields = ["compute_backend", "workflow", "enabled"]
    sortable_field_mapping = {
        "compute_backend": ComputeBackend.name,
        "workflow": Workflow.name,
    }
    fields_default_sort = [("workflow", False)]

    async def repr(self, obj: Any, request: Request) -> str:
        return f"BackendWorkflowSettings({obj.compute_backend.name}, {obj.workflow.name})"


class WorkflowRepoStatusAdmin(ModelView):
    fields = [
        HasOne("compute_backend", identity="compute-backend"),
        HasOne("workflow", identity="workflow"),
        "staged_commit_sha",
        "location",
        "status",
        "transfer_id",
        "created_at",
        "updated_at",
        "id",
    ]
    sortable_fields = ["compute_backend", "workflow", "created_at", "updated_at"]
    fields_default_sort = [("created_at", True)]
    sortable_field_mapping = {
        "compute_backend": ComputeBackend.name,
        "workflow": Workflow.name,
    }
