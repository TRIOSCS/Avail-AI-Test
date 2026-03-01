"""NetComponents worker status model (singleton table).

Single-row table that the worker process updates and the API server
reads to display worker health on dashboards. Only one row (id=1)
is allowed via CHECK constraint.

Business Rules:
- Exactly one row exists (id=1), inserted by migration
- Worker updates this row periodically with heartbeat and stats
- API server reads it for /api/nc/worker/health endpoint

Called by: nc_worker.worker (heartbeat updates), nc_admin router (reads)
Depends on: nothing (standalone table)
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, Integer, Text

from .base import Base


class NcWorkerStatus(Base):
    __tablename__ = "nc_worker_status"

    id = Column(Integer, primary_key=True, default=1)
    is_running = Column(Boolean, default=False)
    last_heartbeat = Column(DateTime)
    last_search_at = Column(DateTime)
    searches_today = Column(Integer, default=0)
    sightings_today = Column(Integer, default=0)
    circuit_breaker_open = Column(Boolean, default=False)
    circuit_breaker_reason = Column(Text)
    daily_stats_json = Column(JSON)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (CheckConstraint("id = 1", name="ck_nc_worker_status_singleton"),)
