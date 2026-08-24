"""Database setup for SQLAlchemy and Alembic."""

from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ..config import get_settings

# Naming convention for constraints (recommended by Alembic)
# https://alembic.sqlalchemy.org/en/latest/naming.html
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


def _get_database_url() -> str:
    settings = get_settings()
    return settings.database_url


def _get_engine_options(database_url: str) -> dict[str, Any]:
    settings = get_settings()
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite:"):
        return options

    options.update(
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
    return options


database_url = _get_database_url()
engine = create_engine(database_url, **_get_engine_options(database_url))
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
