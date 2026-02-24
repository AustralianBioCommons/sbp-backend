"""Starlette Admin and DB debug API mounting helpers."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest
from starlette.responses import HTMLResponse, Response
from starlette_admin._types import RequestAction
from starlette_admin.auth import AdminUser, AuthProvider, LoginFailed
from starlette_admin.fields import StringField

from ..auth.validator import verify_access_token_claims
from ..routes.dependencies import get_db
from . import engine
from .models.core import (
    AppUser,
    RunInput,
    RunMetric,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)

DEFAULT_DB_ADMIN_ROLE_CLAIM = "biocommons/role/sbp/admin"
DEFAULT_DB_ADMIN_TOKEN_COOKIE = "sbp_admin_token"


def _is_db_admin_enabled() -> bool:
    return os.getenv("ENABLE_DB_ADMIN", "false").strip().lower() in {"1", "true", "yes"}


def _is_db_admin_cookie_secure() -> bool:
    return os.getenv("DB_ADMIN_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}


def _mask_auth0_user_id(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:8]}{'*' * (len(value) - 12)}{value[-4:]}"


def _mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*" if len(local) == 2 else "*"
    else:
        masked_local = f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"
    return f"{masked_local}@{domain}"


def _is_truthy_claim_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "admin", "sbp_admin"}
    if isinstance(value, (list, tuple, set)):
        return any(_is_truthy_claim_value(item) for item in value)
    return False


def _claims_has_admin_role(claims: dict[str, object]) -> bool:
    required_claim_key = os.getenv("DB_ADMIN_ROLE_CLAIM", DEFAULT_DB_ADMIN_ROLE_CLAIM).strip()
    if not required_claim_key:
        return False

    direct_value = claims.get(required_claim_key)
    if _is_truthy_claim_value(direct_value):
        return True

    permissions = claims.get("permissions")
    if isinstance(permissions, list) and required_claim_key in permissions:
        return True

    roles = claims.get("roles")
    if isinstance(roles, list) and required_claim_key in roles:
        return True

    return False


def _extract_admin_token_from_request(request: StarletteRequest) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    cookie_name = os.getenv("DB_ADMIN_TOKEN_COOKIE", DEFAULT_DB_ADMIN_TOKEN_COOKIE).strip()
    if cookie_name:
        cookie_token = request.cookies.get(cookie_name)
        if isinstance(cookie_token, str) and cookie_token.strip():
            return cookie_token.strip()
    return None


def _verify_admin_request(request: StarletteRequest) -> dict[str, object]:
    token = _extract_admin_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )

    claims = verify_access_token_claims(token)
    if not _claims_has_admin_role(claims):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role is required",
        )
    return claims


def require_admin_access(request: StarletteRequest) -> dict[str, object]:
    return _verify_admin_request(request)


def mount_db_admin(app: FastAPI) -> None:
    """Mount Starlette Admin and read-only debug endpoints when enabled."""
    if not _is_db_admin_enabled():
        return

    _mount_starlette_admin(app)
    _mount_db_debug_api(app)


def _mount_starlette_admin(app: FastAPI) -> None:
    try:
        from starlette_admin.contrib.sqla import Admin, ModelView
    except ImportError as exc:  # pragma: no cover - dependency issue
        raise RuntimeError(
            "ENABLE_DB_ADMIN=true but starlette-admin is not installed."
        ) from exc

    cookie_name = os.getenv("DB_ADMIN_TOKEN_COOKIE", DEFAULT_DB_ADMIN_TOKEN_COOKIE).strip()

    class Auth0AdminAuthProvider(AuthProvider):
        async def login(
            self,
            username: str,
            password: str,
            remember_me: bool,
            request: StarletteRequest,
            response: Response,
        ) -> Response:
            _ = (password, remember_me)
            token = (username or "").strip()
            if not token:
                raise LoginFailed("Please provide an Auth0 access token.")
            try:
                claims = verify_access_token_claims(token)
            except HTTPException as exc:
                raise LoginFailed(str(exc.detail)) from exc

            if not _claims_has_admin_role(claims):
                raise LoginFailed(
                    f"Missing required admin role claim: {os.getenv('DB_ADMIN_ROLE_CLAIM', DEFAULT_DB_ADMIN_ROLE_CLAIM)}"
                )

            response.set_cookie(
                key=cookie_name,
                value=token,
                httponly=True,
                secure=_is_db_admin_cookie_secure(),
                samesite="lax",
                path="/admin",
            )
            return response

        async def logout(self, request: StarletteRequest, response: Response) -> Response:
            _ = request
            response.delete_cookie(key=cookie_name, path="/admin")
            return response

        async def render_login(self, request: StarletteRequest, admin: object) -> Response:
            _ = admin
            if request.method == "GET":
                html = """
                <html>
                  <head><title>SBP Admin Login</title></head>
                  <body>
                    <h2>SBP Admin Login</h2>
                    <p>Paste a valid Auth0 access token with admin role.</p>
                    <form method="post">
                      <label for="username">Access Token</label><br/>
                      <input id="username" name="username" type="password" style="width: 640px;" /><br/><br/>
                      <input name="password" type="hidden" value="unused" />
                      <button type="submit">Login</button>
                    </form>
                  </body>
                </html>
                """
                return HTMLResponse(html)
            return await super().render_login(request, admin)

        async def is_authenticated(self, request: StarletteRequest) -> bool:
            try:
                claims = _verify_admin_request(request)
            except HTTPException:
                return False

            request.state.user = claims
            return True

        def get_admin_user(self, request: StarletteRequest) -> AdminUser | None:
            claims = getattr(request.state, "user", None)
            if not isinstance(claims, dict):
                return None
            username = str(
                claims.get("name")
                or claims.get("nickname")
                or claims.get("email")
                or claims.get("sub")
                or "Administrator"
            )
            return AdminUser(username=username)

    class MaskedAuth0UserIdField(StringField):
        async def parse_obj(self, request: StarletteRequest, obj: object) -> str | None:
            raw_value = getattr(obj, self.name, None)
            return _mask_auth0_user_id(str(raw_value) if raw_value is not None else None)

        async def serialize_value(
            self, request: StarletteRequest, value: object, action: RequestAction
        ) -> str | None:
            return _mask_auth0_user_id(str(value) if value is not None else None)

    class MaskedEmailField(StringField):
        async def parse_obj(self, request: StarletteRequest, obj: object) -> str | None:
            raw_value = getattr(obj, self.name, None)
            return _mask_email(str(raw_value) if raw_value is not None else None)

        async def serialize_value(
            self, request: StarletteRequest, value: object, action: RequestAction
        ) -> str | None:
            return _mask_email(str(value) if value is not None else None)

    class AppUserAdmin(ModelView):
        fields = [
            "id",
            MaskedAuth0UserIdField(
                "auth0_user_id",
                label="Auth0 User ID",
                read_only=True,
                exclude_from_create=True,
                exclude_from_edit=True,
            ),
            "name",
            MaskedEmailField(
                "email",
                label="Email",
                read_only=True,
                exclude_from_create=True,
                exclude_from_edit=True,
            ),
        ]

    class WorkflowAdmin(ModelView):
        fields = ["id", "name", "description", "repo_url", "default_revision"]

    class WorkflowRunAdmin(ModelView):
        fields = [
            "id",
            "workflow_id",
            "owner_user_id",
            "seqera_dataset_id",
            "seqera_run_id",
            "run_name",
            "binder_name",
            "work_dir",
        ]

    class RunMetricAdmin(ModelView):
        class RunIdField(StringField):
            async def parse_obj(self, request: StarletteRequest, obj: object) -> str | None:
                raw_value = getattr(obj, self.name, None)
                return str(raw_value) if raw_value is not None else None

            async def serialize_value(
                self, request: StarletteRequest, value: object, action: RequestAction
            ) -> str | None:
                return str(value) if value is not None else None

        fields = [
            RunIdField(
                "run_id",
                label="Run ID",
                read_only=True,
                exclude_from_create=True,
                exclude_from_edit=True,
            ),
            "max_score",
        ]

    def _has_column(model: type, column_name: str) -> bool:
        return column_name in model.__table__.columns.keys()

    if _has_column(WorkflowRun, "sample_id"):
        WorkflowRunAdmin.fields.insert(-1, "sample_id")

    if _has_column(RunMetric, "final_design_count"):
        RunMetricAdmin.fields.append("final_design_count")

    admin = Admin(
        engine=engine,
        title=os.getenv("DB_ADMIN_TITLE", "SBP Backend Admin"),
        auth_provider=Auth0AdminAuthProvider(),
    )
    admin.add_view(AppUserAdmin(AppUser))
    admin.add_view(WorkflowAdmin(Workflow))
    admin.add_view(WorkflowRunAdmin(WorkflowRun))
    admin.add_view(RunMetricAdmin(RunMetric))
    admin.mount_to(app)


def _mount_db_debug_api(app: FastAPI) -> None:
    router = APIRouter(
        prefix="/admin/debug",
        tags=["admin-debug"],
        dependencies=[Depends(require_admin_access)],
    )

    @router.get("/s3-objects")
    def list_s3_objects(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(S3Object)).scalar_one()
        rows = db.execute(
            select(S3Object).order_by(S3Object.object_key).offset(offset).limit(limit)
        ).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "object_key": row.object_key,
                    "uri": row.uri,
                    "version_id": row.version_id,
                    "size_bytes": row.size_bytes,
                }
                for row in rows
            ],
        }

    @router.get("/run-inputs")
    def list_run_inputs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(RunInput)).scalar_one()
        rows = db.execute(select(RunInput).offset(offset).limit(limit)).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "run_id": str(row.run_id),
                    "s3_object_id": row.s3_object_id,
                }
                for row in rows
            ],
        }

    @router.get("/run-outputs")
    def list_run_outputs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(RunOutput)).scalar_one()
        rows = db.execute(select(RunOutput).offset(offset).limit(limit)).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "run_id": str(row.run_id),
                    "s3_object_id": row.s3_object_id,
                }
                for row in rows
            ],
        }

    app.include_router(router)
