"""Database setup for SQLAlchemy and Alembic."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def _get_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/sbp")


engine = create_engine(_get_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
