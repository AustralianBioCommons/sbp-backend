"""Database-backed cache and history for runtime system health."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...schemas.health import SystemStatus
from .. import Base


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


class SystemStatusIncident(Base):
    """
    One row per downtime period for a monitored component: opened when a probe
    first reports ``degraded``/``unhealthy``, closed when it next reports
    ``healthy``. Healthy checks are never recorded, so storage scales with the
    number of actual outages rather than with polling frequency.
    """

    __tablename__ = "system_status_incidents"
    __table_args__ = (
        Index("ix_system_status_incidents_component_started_at", "component", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
