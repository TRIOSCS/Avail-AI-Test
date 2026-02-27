"""NetComponents search log model.

Audit trail for every NC search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.

Called by: nc_worker worker loop, sighting_writer
Depends on: nc_search_queue table
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from .base import Base


class NcSearchLog(Base):
    __tablename__ = "nc_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(
        Integer, ForeignKey("nc_search_queue.id", ondelete="CASCADE"), nullable=False
    )
    searched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    duration_ms = Column(Integer)
    results_found = Column(Integer)
    sightings_created = Column(Integer)
    page_html_hash = Column(String(64))
    error = Column(Text)
