"""Enrichment worker status model (singleton table).

Single-row table that the worker process updates and the API server
reads to display worker health on dashboards. Only one row (id=1)
is allowed via CHECK constraint.

Business Rules:
- Exactly one row exists (id=1), inserted by migration
- Worker updates this row periodically with heartbeat and stats
- Tracks daily enrichment counts by tier (web_sourced, oem_sourced, ai_inferred,
  not_found, not_catalogued)
- Circuit breaker state is persisted here for observability

Called by: enrichment_worker.worker (heartbeat updates)
Depends on: nothing (standalone table)
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, Integer, Text

from ..database import UTCDateTime
from .base import Base


class EnrichmentWorkerStatus(Base):
    __tablename__ = "enrichment_worker_status"

    id = Column(Integer, primary_key=True, default=1)
    is_running = Column(Boolean, default=False, server_default="false", nullable=False)
    last_heartbeat = Column(UTCDateTime)
    last_enriched_at = Column(UTCDateTime)
    enriched_today = Column(Integer, default=0, server_default="0", nullable=False)
    web_sourced_today = Column(Integer, default=0, server_default="0", nullable=False)
    ai_inferred_today = Column(Integer, default=0, server_default="0", nullable=False)
    not_found_today = Column(Integer, default=0, server_default="0", nullable=False)
    oem_sourced_today = Column(Integer, default=0, server_default="0", nullable=False)
    not_catalogued_today = Column(Integer, default=0, server_default="0", nullable=False)
    circuit_breaker_open = Column(Boolean, default=False, server_default="false", nullable=False)
    circuit_breaker_reason = Column(Text)
    daily_stats_json = Column(JSON)
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_enrichment_worker_status_singleton"),)


def update_enrichment_worker_status(db, **kwargs) -> None:
    """Update the enrichment_worker_status singleton row (id=1).

    Pass any column as a kwarg: is_running=True, enriched_today=5, etc.
    Silently returns if the row does not yet exist (e.g. pre-migration).
    """
    status = db.get(EnrichmentWorkerStatus, 1)
    if not status:
        return
    for key, value in kwargs.items():
        if hasattr(status, key):
            setattr(status, key, value)
    status.updated_at = datetime.now(timezone.utc)
    db.commit()
