"""Sync models — sync log tracking."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, Index, Integer, String

from .base import Base


class SyncLog(Base):
    """Log of each data sync run."""

    __tablename__ = "sync_logs"
    id = Column(Integer, primary_key=True)
    source = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    duration_seconds = Column(Float)
    row_counts = Column(JSON)
    errors = Column(JSON)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_sync_source_time", "source", "started_at"),)
