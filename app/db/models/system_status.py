"""Database-backed cache for runtime system health."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from .. import Base
from ...schemas.health import SystemStatus


class SystemStatusCache(Base):
    """
    Stores current system health status in the DB, so it can be shared across
    processes. There should be only one row in the table,
    with all systems using a common key.
    """

    __tablename__ = "system_status_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def is_fresh(self, now: datetime) -> bool:
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now < expires_at

    def get_status(self) -> SystemStatus:
        """
        Return a SystemStatus object from the cache payload.
        """
        return SystemStatus.model_validate(self.payload)
