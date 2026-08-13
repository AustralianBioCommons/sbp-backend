from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field, HttpUrl, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def validate_url(s: str) -> str:
    http_url = TypeAdapter(HttpUrl)
    http_url.validate_python(s)
    return s


def split_by_comma(v: str | list[str]) -> list[str]:
    if isinstance(v, str):
        result = []
        for item in v.split(","):
            if item.strip():
                result.append(item.strip())
        if not result:
            raise ValueError(f"List is empty after splitting: {v}")
        return result
    return v


UrlStr = Annotated[str, AfterValidator(validate_url)]


class NestedSettings(BaseSettings):
    """
    Used to declare nested settings with their own
    prefix
    """

    model_config = SettingsConfigDict(env_file=".env", dotenv_filtering="match_prefix")


class SeqeraSettings(NestedSettings):
    api_url: UrlStr
    access_token: str
    compute_id: str
    work_space: str
    work_dir: str
    enable_agent_healthcheck: bool = False
    healthcheck_agent_timeout_seconds: int = 20
    health_cache_ttl_seconds: int = 30
    max_concurrent_workflows: int = 25
    workflow_sync_batch_limit: int = 50

    @field_validator("work_dir")
    @classmethod
    def normalize_work_dir(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized:
            raise ValueError("work_dir must not be empty")
        return normalized

    model_config = SettingsConfigDict(env_prefix="SEQERA_")


class AwsSettings(NestedSettings):
    access_key_id: str
    secret_access_key: str
    region: str
    s3_bucket: str
    log_group: str | None = None

    model_config = SettingsConfigDict(env_prefix="AWS_")


class AdminSettings(NestedSettings):
    auth_redirect_uri: UrlStr
    forbidden_home_url: UrlStr
    cookie_secure: bool
    session_secret: str
    roles_claim: str

    model_config = SettingsConfigDict(env_prefix="DB_ADMIN_")


class HpcSettings(NestedSettings):
    gadi_project: str = "yz52"
    max_concurrent_workflows: int = 25

    model_config = SettingsConfigDict(env_prefix="HPC_")


class AuthSettings(NestedSettings):
    # domain without http or slashes e.g. dev.biocommons.org.au
    domain: str
    client_id: str
    client_secret: str | None = None
    # Audience for access tokens, e.g. https://dev.biocommons.org.au
    audience: str
    issuer: str | None = None
    algorithms: Annotated[list[str], NoDecode, BeforeValidator(split_by_comma)] = ["RS256"]
    # TODO: Is this still used, in addition to DB_ADMIN_AUTH_REDIRECT_URI?
    redirect_uri: UrlStr
    required_role: str
    workflow_execution_role: str
    workflow_sync_batch_limit: int = 50

    model_config = SettingsConfigDict(env_prefix="AUTH_")


class Settings(BaseSettings):
    """
    Core settings for the app.

    Settings for specific areas are nested under
    seqera, aws, etc.
    """

    # Comma-separated list of allowed origins for CORS
    allowed_origins: Annotated[list[str], NoDecode, BeforeValidator(split_by_comma)]
    # Single URL including username and password
    database_url: str
    port: int
    uvicorn_reload: bool = False
    enable_db_admin: bool = False
    enable_credits: bool = False

    seqera: SeqeraSettings = Field(default_factory=SeqeraSettings)
    aws: AwsSettings = Field(default_factory=AwsSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    model_config = SettingsConfigDict(env_file=".env", dotenv_filtering="only_existing")


@lru_cache
def get_settings() -> Settings:
    """
    Load settings from environment variables/env file.

    This should be used throughout the app instead of
    creating Settings() directly, to allow overriding
    etc.
    """
    return Settings()
