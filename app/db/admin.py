"""Starlette Admin and DB debug API mounting helpers."""

# mypy: ignore-errors

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from time import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy import inspect as sqla_inspect
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.requests import Request as StarletteRequest
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette_admin import CustomView, DropDown, HasMany, JSONField, TimezoneConfig
from starlette_admin._types import RequestAction
from starlette_admin.actions import link_row_action
from starlette_admin.auth import AdminUser, AuthProvider, LoginFailed
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.fields import HasOne, StringField

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
from .models.job_queue import QueuedJob

_ADMIN_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)

DEFAULT_DB_ADMIN_REQUIRED_ROLE = "biocommons/role/sbp/admin"
DEFAULT_DB_ADMIN_ROLES_CLAIM = "https://biocommons.org.au/roles"
DEFAULT_DB_ADMIN_SESSION_COOKIE = "sbp_admin_session"

# Fallback actor recorded on app_users.credit_updated_by for credit changes made
# through the Starlette Admin database dashboard. The signed-in admin's email is
# recorded when available; this label is only used when the email is unknown.
DB_ADMIN_CREDIT_ACTOR = "admin dashboard"

# Timestamps are stored in the DB as UTC (DateTime(timezone=True)). The admin
# always displays them in Sydney/Melbourne time (AEST/AEDT), regardless of the
# viewer's location.
DB_ADMIN_DISPLAY_TIMEZONE = "Australia/Sydney"


def _encode_admin_pk(value: object) -> str:
    raw = str(value).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_admin_pk(value: object) -> str:
    raw = str(value)
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding).decode("utf-8")


class AppUserAdmin(ModelView):
    fields = [
        "id",
        "auth0_user_id",
        "name",
        "email",
        "credit",
        "credit_updated_at",
        "credit_updated_by",
    ]

    # credit_updated_at / credit_updated_by are stamped automatically whenever a
    # credit balance is changed through the dashboard, so they are read-only:
    # shown in the list/detail views but excluded from the create/edit forms.
    exclude_fields_from_create = ("credit_updated_at", "credit_updated_by")
    exclude_fields_from_edit = ("credit_updated_at", "credit_updated_by")

    @staticmethod
    def _credit_actor(request: Request | None) -> str:
        # Prefer the signed-in admin's email (placed on request.state.user by the
        # auth provider); fall back to the generic label when it is unavailable.
        claims = getattr(getattr(request, "state", None), "user", None)
        if isinstance(claims, dict):
            email = claims.get("email")
            if isinstance(email, str) and email.strip():
                return email.strip()
        return DB_ADMIN_CREDIT_ACTOR

    @staticmethod
    def _stamp_credit_audit(obj: Any, actor: str) -> None:
        obj.credit_updated_at = datetime.now(UTC)
        obj.credit_updated_by = actor

    async def before_create(self, request: Request, data: dict[str, Any], obj: Any) -> None:
        # Only record an audit trail when the new user starts with a credit balance.
        if getattr(obj, "credit", 0):
            self._stamp_credit_audit(obj, self._credit_actor(request))

    async def before_edit(self, request: Request, data: dict[str, Any], obj: Any) -> None:
        # obj has already been populated with the submitted form values; SQLAlchemy
        # change tracking tells us whether the credit column actually changed.
        if sqla_inspect(obj).attrs.credit.history.has_changes():
            self._stamp_credit_audit(obj, self._credit_actor(request))

    async def repr(self, obj: Any, request: Request) -> str:
        return f"{obj.email}"


class WorkflowAdmin(ModelView):
    fields = [
        "id",
        "name",
        "description",
        "repo_url",
        "default_revision",
        "config_path",
        "prerun_script_path",
    ]

    async def before_save(self, request: Request, obj: Any, is_created: bool) -> None:
        nullable_fields = (
            "description",
            "repo_url",
            "default_revision",
            "config_path",
            "prerun_script_path",
        )
        for field in nullable_fields:
            value = getattr(obj, field, None)
            if isinstance(value, str) and not value.strip():
                setattr(obj, field, None)

    async def repr(self, obj: Any, request: Request) -> str:
        return f"{obj.name}"


class WorkflowRunAdmin(ModelView):
    fields = [
        "id",
        "workflow_id",
        "tool",
        "owner_user_id",
        HasOne("workflow", identity="workflow"),
        HasOne("owner", identity="app-user"),
        "seqera_dataset_id",
        "seqera_run_id",
        "run_name",
        "binder_name",
        JSONField("submitted_form_data"),
        "work_dir",
        "submission_timestamp",
    ]
    exclude_fields_from_list = "submitted_form_data"

    async def repr(self, obj: Any, request: Request) -> str:
        return f"{obj.run_name}"


class RunMetricAdmin(ModelView):
    fields = [
        HasOne("run", identity="workflow-run"),
        "max_score",
    ]


class UrlSafePrimaryKeyModelView(ModelView):
    serialize_pk_field_as_raw = False

    async def get_pk_value(self, request: Request, obj: Any) -> str:
        raw_pk = await super().get_pk_value(request, obj)
        return _encode_admin_pk(raw_pk)

    async def find_by_pk(self, request: Request, pk: Any) -> Any:
        return await super().find_by_pk(request, _decode_admin_pk(pk))

    async def find_by_pks(self, request: Request, pks: list[Any]) -> list[Any]:
        return await super().find_by_pks(request, [_decode_admin_pk(pk) for pk in pks])

    def _row_action_pk(self, pk: Any) -> str:
        if self.serialize_pk_field_as_raw:
            return _encode_admin_pk(pk)
        return str(pk)

    @link_row_action(
        name="view",
        text="View",
        icon_class="fa-solid fa-eye",
        exclude_from_detail=True,
    )
    def row_action_1_view(self, request: Request, pk: Any) -> str:
        route_name = request.app.state.ROUTE_NAME
        return str(
            request.url_for(
                route_name + ":detail",
                identity=self.identity,
                pk=self._row_action_pk(pk),
            )
        )

    @link_row_action(
        name="edit",
        text="Edit",
        icon_class="fa-solid fa-edit",
        action_btn_class="btn-primary",
    )
    def row_action_2_edit(self, request: Request, pk: Any) -> str:
        route_name = request.app.state.ROUTE_NAME
        return str(
            request.url_for(
                route_name + ":edit",
                identity=self.identity,
                pk=self._row_action_pk(pk),
            )
        )


class S3ObjectAdmin(UrlSafePrimaryKeyModelView):
    serialize_pk_field_as_raw = True

    fields = [
        "object_key",
        "uri",
        HasMany("run_inputs", identity="run-input"),
        HasMany("run_outputs", identity="run-output"),
        "version_id",
        "size_bytes",
    ]

    async def repr(self, obj: Any, request: Request) -> str:
        return str(obj.object_key)


class RunInputAdmin(UrlSafePrimaryKeyModelView):
    fields = [
        HasOne("run", identity="workflow-run"),
        HasOne("s3_object", identity="s3-object"),
    ]


class RunOutputAdmin(UrlSafePrimaryKeyModelView):
    fields = [
        HasOne("run", identity="workflow-run"),
        HasOne("s3_object", identity="s3-object"),
    ]


class QueuedJobAdmin(ModelView):
    fields = [
        "id",
        HasOne("workflow_run", identity="workflow-run"),
        HasOne("workflow", identity="workflow"),
        "launch_payload",
        "status",
        "attempts",
        "queued_at",
        "last_attempt_at",
        "next_attempt_at",
        "submitted_at",
        "error",
    ]


def _is_db_admin_enabled() -> bool:
    return os.getenv("ENABLE_DB_ADMIN", "false").strip().lower() in {"1", "true", "yes"}


def _is_db_admin_cookie_secure() -> bool:
    return os.getenv("DB_ADMIN_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}


def _get_db_admin_home_url() -> str:
    return os.getenv("DB_ADMIN_FORBIDDEN_HOME_URL", "/").strip() or "/"


def _get_admin_auth_domain() -> str | None:
    value = os.getenv("AUTH_DOMAIN", "").strip()
    return value or None


def _get_admin_auth_client_id() -> str | None:
    value = os.getenv("AUTH_CLIENT_ID", "").strip()
    return value or None


def _get_admin_auth_audience() -> str | None:
    value = os.getenv("AUTH_AUDIENCE", "").strip()
    return value or None


def _get_admin_session_cookie_name() -> str:
    return DEFAULT_DB_ADMIN_SESSION_COOKIE


def _get_admin_session_secret() -> str:
    value = os.getenv("DB_ADMIN_SESSION_SECRET")
    if value and value.strip():
        return value.strip()
    raise RuntimeError("DB_ADMIN_SESSION_SECRET is required when ENABLE_DB_ADMIN=true")


def _validate_db_admin_config() -> None:
    missing: list[str] = []
    if not _get_admin_auth_domain():
        missing.append("AUTH_DOMAIN")
    if not _get_admin_auth_client_id():
        missing.append("AUTH_CLIENT_ID")
    if not _get_admin_auth_audience():
        missing.append("AUTH_AUDIENCE")
    if not os.getenv("DB_ADMIN_AUTH_REDIRECT_URI", "").strip():
        missing.append("DB_ADMIN_AUTH_REDIRECT_URI")
    if not os.getenv("DB_ADMIN_SESSION_SECRET", "").strip():
        missing.append("DB_ADMIN_SESSION_SECRET")

    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            f"ENABLE_DB_ADMIN=true but required DB admin env vars are missing: {missing_text}"
        )


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


def _claims_has_admin_role(claims: dict[str, object]) -> bool:
    required_role = os.getenv("DB_ADMIN_REQUIRED_ROLE", DEFAULT_DB_ADMIN_REQUIRED_ROLE).strip()
    roles_claim_name = os.getenv("DB_ADMIN_ROLES_CLAIM", DEFAULT_DB_ADMIN_ROLES_CLAIM).strip()
    if not required_role or not roles_claim_name:
        return False

    claim_value = claims.get(roles_claim_name)
    if not isinstance(claim_value, (list, tuple, set)):
        return False

    return any(str(item).strip() == required_role for item in claim_value)


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

    _validate_db_admin_config()
    # Register the debug JSON API router BEFORE mounting Starlette Admin. The admin
    # is mounted as a greedy Mount("/admin") that would otherwise shadow any
    # /admin/* APIRoute added after it (routes are matched in registration order).
    # Note: the /admin/api/system-status router is registered in main.py (also
    # before this mount) so it stays available independently of the dashboard.
    _mount_db_debug_api(app)
    _mount_starlette_admin(app)


def _mount_starlette_admin(app: FastAPI) -> None:
    session_cookie_name = _get_admin_session_cookie_name()
    oauth_state_cookie_name = "sbp_admin_oauth_state"
    oauth_verifier_cookie_name = "sbp_admin_oauth_verifier"
    oauth_next_cookie_name = "sbp_admin_oauth_next"

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
                samesite="strict",
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
                auth_base = (
                    auth_domain.rstrip("/")
                    if auth_domain.startswith(("http://", "https://"))
                    else f"https://{auth_domain.strip('/')}"
                )

                # OAuth callback from auth provider: exchange code for access token.
                code = request.query_params.get("code")
                callback_state = request.query_params.get("state")
                if code:
                    expected_state = request.cookies.get(oauth_state_cookie_name)
                    if not expected_state or callback_state != expected_state:
                        raise LoginFailed("Invalid OAuth state.")

                    code_verifier = request.cookies.get(oauth_verifier_cookie_name)
                    if not code_verifier:
                        raise LoginFailed("Missing OAuth verifier.")

                    token_payload = {
                        "grant_type": "authorization_code",
                        "client_id": auth_client_id,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": code_verifier,
                    }
                    client_secret = os.getenv("DB_ADMIN_AUTH_CLIENT_SECRET", "").strip()
                    if client_secret:
                        token_payload["client_secret"] = client_secret

                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            token_resp = await client.post(
                                f"{auth_base}/oauth/token",
                                data=token_payload,
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                            )
                            token_resp.raise_for_status()
                            token_data = token_resp.json()
                    except Exception as exc:
                        raise LoginFailed("Failed to complete OAuth login.") from exc

                    access_token = str(token_data.get("access_token") or "").strip()
                    if not access_token:
                        raise LoginFailed("No access token returned by auth provider.")

                    # Reuse existing token + role validation logic.
                    response = RedirectResponse(
                        url=request.cookies.get(oauth_next_cookie_name) or "/admin/",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                    response = await self.login(
                        username=access_token,
                        password="unused",
                        remember_me=False,
                        request=request,
                        response=response,
                    )
                    response.delete_cookie(key=oauth_state_cookie_name, path="/admin")
                    response.delete_cookie(key=oauth_verifier_cookie_name, path="/admin")
                    response.delete_cookie(key=oauth_next_cookie_name, path="/admin")
                    return response

                # Begin OAuth authorization code flow with PKCE.
                next_url = request.query_params.get("next") or "/admin/"
                if not next_url.startswith("/"):
                    next_url = "/admin/"

                code_verifier = secrets.token_urlsafe(64)
                code_challenge = _b64url_encode(
                    hashlib.sha256(code_verifier.encode("utf-8")).digest()
                )
                oauth_state = secrets.token_urlsafe(32)
                query = urlencode(
                    {
                        "response_type": "code",
                        "client_id": auth_client_id,
                        "audience": auth_audience,
                        "scope": "openid profile email",
                        "redirect_uri": redirect_uri,
                        "state": oauth_state,
                        "code_challenge": code_challenge,
                        "code_challenge_method": "S256",
                    }
                )
                response = RedirectResponse(
                    url=f"{auth_base}/authorize?{query}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
                response.set_cookie(
                    key=oauth_state_cookie_name,
                    value=oauth_state,
                    httponly=True,
                    secure=_is_db_admin_cookie_secure(),
                    # OAuth callback is a cross-site top-level navigation; Lax is required.
                    samesite="lax",
                    path="/admin",
                )
                response.set_cookie(
                    key=oauth_verifier_cookie_name,
                    value=code_verifier,
                    httponly=True,
                    secure=_is_db_admin_cookie_secure(),
                    # OAuth callback is a cross-site top-level navigation; Lax is required.
                    samesite="lax",
                    path="/admin",
                )
                response.set_cookie(
                    key=oauth_next_cookie_name,
                    value=next_url,
                    httponly=True,
                    secure=_is_db_admin_cookie_secure(),
                    # OAuth callback is a cross-site top-level navigation; Lax is required.
                    samesite="lax",
                    path="/admin",
                )
                return response
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

    def _has_column(model: type, column_name: str) -> bool:
        return column_name in model.__table__.columns.keys()

    if _has_column(WorkflowRun, "sample_id"):
        WorkflowRunAdmin.fields.insert(-1, "sample_id")

    if _has_column(RunMetric, "final_design_count"):
        RunMetricAdmin.fields.append("final_design_count")

    admin = Admin(
        engine=engine,
        title=os.getenv("DB_ADMIN_TITLE", "SBP Backend Admin"),
        templates_dir=_ADMIN_TEMPLATES_DIR,
        auth_provider=Auth0AdminAuthProvider(),
        # Timestamps are stored as UTC; always display them in Sydney/Melbourne
        # time. Browser-timezone auto-detection (use_user_locale_timezone) and the
        # timezone cookie override are both disabled so the displayed timezone is
        # fixed regardless of where the viewer is. timezone_switcher is left unset:
        # the navbar switcher widget requires the optional `babel` dependency and
        # would 500 the admin pages without it.
        timezone_config=TimezoneConfig(
            default_timezone=DB_ADMIN_DISPLAY_TIMEZONE,
            database_timezone="UTC",
            use_user_locale_timezone=False,
            timezone_cookie_name=None,
        ),
    )
    admin.add_view(AppUserAdmin(AppUser))
    admin.add_view(WorkflowAdmin(Workflow))
    admin.add_view(WorkflowRunAdmin(WorkflowRun))
    admin.add_view(RunMetricAdmin(RunMetric))
    admin.add_view(RunInputAdmin(RunInput))
    admin.add_view(RunOutputAdmin(RunOutput))
    admin.add_view(S3ObjectAdmin(S3Object))
    admin.add_view(DropDown("Job queue", [QueuedJobAdmin(QueuedJob)]))
    admin.add_view(
        CustomView(
            label="System Status",
            icon="fa-solid fa-heart-pulse",
            path="/system-status",
            template_path="admin/system_status.html",
            name="system-status",
        )
    )
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
