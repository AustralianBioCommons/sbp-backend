"""Starlette Admin and DB debug API mounting helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from time import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest
from starlette.responses import HTMLResponse, Response
from starlette_admin._types import RequestAction
from starlette_admin.auth import AdminUser, AuthProvider, LoginFailed
from starlette_admin.fields import StringField

from ..auth.validator import fetch_userinfo_claims, verify_access_token_claims
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
DEFAULT_DB_ADMIN_SESSION_COOKIE = "sbp_admin_session"
DEFAULT_DB_ADMIN_AUTH_DOMAIN = "dev.login.aai.test.biocommons.org.au"
DEFAULT_DB_ADMIN_AUTH_AUDIENCE = "https://dev.api.aai.test.biocommons.org.au"
DEFAULT_DB_ADMIN_AUTH_CLIENT_ID = "VgTSGK8Ph92r8mVhmVvQDrxGzbWX0vCm"


def _is_db_admin_enabled() -> bool:
    return os.getenv("ENABLE_DB_ADMIN", "false").strip().lower() in {"1", "true", "yes"}


def _is_db_admin_cookie_secure() -> bool:
    return os.getenv("DB_ADMIN_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}


def _get_db_admin_home_url() -> str:
    return (
        os.getenv("DB_ADMIN_FORBIDDEN_HOME_URL", "https://dev.sbp.test.biocommons.org.au/").strip()
        or "https://dev.sbp.test.biocommons.org.au/"
    )


def _get_admin_auth_domain() -> str | None:
    value = (
        os.getenv("DB_ADMIN_AUTH_DOMAIN")
        or os.getenv("AUTH0_DOMAIN")
        or DEFAULT_DB_ADMIN_AUTH_DOMAIN
    )
    if not value:
        return None
    return value.strip()


def _get_admin_auth_client_id() -> str | None:
    value = os.getenv("DB_ADMIN_AUTH_CLIENT_ID") or DEFAULT_DB_ADMIN_AUTH_CLIENT_ID
    if not value:
        return None
    return value.strip()


def _get_admin_auth_audience() -> str | None:
    value = (
        os.getenv("DB_ADMIN_AUTH_AUDIENCE")
        or os.getenv("AUTH0_AUDIENCE")
        or DEFAULT_DB_ADMIN_AUTH_AUDIENCE
    )
    if not value:
        return None
    return value.strip()


def _get_admin_session_cookie_name() -> str:
    return DEFAULT_DB_ADMIN_SESSION_COOKIE


def _get_admin_session_secret() -> str:
    value = os.getenv("DB_ADMIN_SESSION_SECRET")
    if value and value.strip():
        return value.strip()
    # Fallback for local/dev. Set DB_ADMIN_SESSION_SECRET in deployed environments.
    return "sbp-admin-dev-session-secret-change-me"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _create_admin_session_value(claims: dict[str, object]) -> str:
    now = int(time())
    exp_claim = claims.get("exp")
    if isinstance(exp_claim, (int, float)):
        exp = int(exp_claim)
    else:
        exp = now + 3600

    payload = {
        "sub": claims.get("sub"),
        "name": claims.get("name"),
        "nickname": claims.get("nickname"),
        "email": claims.get("email"),
        "exp": exp,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_json)
    signature = hmac.new(
        _get_admin_session_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def _parse_admin_session_value(value: str) -> dict[str, object] | None:
    if not value or "." not in value:
        return None
    payload_b64, signature = value.rsplit(".", 1)
    expected_signature = hmac.new(
        _get_admin_session_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        payload_raw = _b64url_decode(payload_b64)
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp_claim = payload.get("exp")
    if not isinstance(exp_claim, (int, float)):
        return None
    if float(exp_claim) <= time():
        return None
    return payload


def _build_display_name_from_claims(claims: dict[str, object]) -> str:
    for key in ("name", "nickname", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    given = claims.get("given_name")
    family = claims.get("family_name")
    given_text = given.strip() if isinstance(given, str) else ""
    family_text = family.strip() if isinstance(family, str) else ""
    full_name = f"{given_text} {family_text}".strip()
    if full_name:
        return full_name

    return "Administrator"


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


def _claim_contains_required_role(value: object, required_role: str) -> bool:
    if isinstance(value, str):
        return value.strip() == required_role
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() == required_role for item in value)
    return False


def _claims_has_admin_role(claims: dict[str, object]) -> bool:
    required_role = os.getenv("DB_ADMIN_ROLE_CLAIM", DEFAULT_DB_ADMIN_ROLE_CLAIM).strip()
    if not required_role:
        return False

    # 1) Direct custom claim as boolean/string/list.
    direct_value = claims.get(required_role)
    if _is_truthy_claim_value(direct_value) or _claim_contains_required_role(
        direct_value, required_role
    ):
        return True

    # 2) Standard top-level arrays.
    permissions = claims.get("permissions")
    if _claim_contains_required_role(permissions, required_role):
        return True

    roles = claims.get("roles")
    if _claim_contains_required_role(roles, required_role):
        return True

    # 3) Namespaced roles/permissions claims (common in Auth0 Actions).
    for claim_key, claim_value in claims.items():
        if not isinstance(claim_key, str):
            continue
        key = claim_key.lower()
        if key.endswith("/roles") or key.endswith("/permissions"):
            if _claim_contains_required_role(claim_value, required_role):
                return True

    return False


def _extract_admin_token_from_request(request: StarletteRequest) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    return None


def _verify_admin_request(request: StarletteRequest) -> dict[str, object]:
    token = _extract_admin_token_from_request(request)
    if token:
        claims = verify_access_token_claims(token)
        if not _claims_has_admin_role(claims):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        return claims

    session_cookie = request.cookies.get(_get_admin_session_cookie_name())
    session_claims = (
        _parse_admin_session_value(session_cookie) if isinstance(session_cookie, str) else None
    )
    if not session_claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    return session_claims


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

    session_cookie_name = _get_admin_session_cookie_name()

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

            # Prefer human-readable identity in admin navbar.
            if not isinstance(claims.get("name"), str) or not str(claims.get("name")).strip():
                userinfo = fetch_userinfo_claims(token)
                if isinstance(userinfo, dict):
                    for key in ("name", "nickname", "email", "given_name", "family_name"):
                        value = userinfo.get(key)
                        if isinstance(value, str) and value.strip() and key not in claims:
                            claims[key] = value.strip()

            if not _claims_has_admin_role(claims):
                home_url = _get_db_admin_home_url()
                return HTMLResponse(
                    f"""
                    <html>
                      <head><title>Forbidden</title></head>
                      <body>
                        <h2>Forbidden</h2>
                        <p>You do not have permission to access this page.</p>
                        <a href="{home_url}">Take me home</a>
                      </body>
                    </html>
                    """,
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            response.set_cookie(
                key=session_cookie_name,
                value=_create_admin_session_value(claims),
                httponly=True,
                secure=_is_db_admin_cookie_secure(),
                samesite="lax",
                path="/admin",
            )
            return response

        async def logout(self, request: StarletteRequest, response: Response) -> Response:
            _ = request
            response.delete_cookie(key=session_cookie_name, path="/admin")
            return response

        async def render_login(self, request: StarletteRequest, admin: object) -> Response:
            _ = admin
            if request.method == "GET":
                auth_domain = _get_admin_auth_domain()
                auth_client_id = _get_admin_auth_client_id()
                auth_audience = _get_admin_auth_audience()
                redirect_uri = os.getenv("DB_ADMIN_AUTH_REDIRECT_URI") or str(request.url)
                auth_url: str | None = None
                if auth_domain and auth_client_id and auth_audience:
                    query = urlencode(
                        {
                            "response_type": "token",
                            "client_id": auth_client_id,
                            "audience": auth_audience,
                            "scope": "openid profile email",
                            "redirect_uri": redirect_uri,
                        }
                    )
                    auth_url = f"https://{auth_domain}/authorize?{query}"

                if auth_url:
                    html = f"""
                    <html>
                      <head><title>SBP Admin Login</title></head>
                      <body>
                        <h2>SBP Admin Login</h2>
                        <p>Redirecting to AAI login...</p>
                        <form id="token-form" method="post">
                          <input id="username" name="username" type="hidden" />
                          <input name="password" type="hidden" value="unused" />
                        </form>
                        <script>
                          (function () {{
                            const hash = window.location.hash || "";
                            const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
                            const token = params.get("access_token");
                            if (token) {{
                              document.getElementById("username").value = token;
                              document.getElementById("token-form").submit();
                              return;
                            }}
                            window.location.assign("{auth_url}");
                          }})();
                        </script>
                      </body>
                    </html>
                    """
                else:
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
            except HTTPException as exc:
                if exc.status_code == status.HTTP_403_FORBIDDEN:
                    raise
                return False

            request.state.user = claims
            return True

        def get_admin_user(self, request: StarletteRequest) -> AdminUser | None:
            claims = getattr(request.state, "user", None)
            if not isinstance(claims, dict):
                return None
            return AdminUser(username=_build_display_name_from_claims(claims))

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
