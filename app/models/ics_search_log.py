"""ICsource search log model.

Audit trail for every ICS search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.

Called by: ics_worker worker loop, sighting_writer
Depends on: ics_search_queue table
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from .base import Base


class IcsSearchLog(Base):
    __tablename__ = "ics_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(
        Integer, ForeignKey("ics_search_queue.id", ondelete="CASCADE"), nullable=False
    )
    searched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    duration_ms = Column(Integer)
    results_found = Column(Integer)
    sightings_created = Column(Integer)
    page_html_hash = Column(String(64))
    error = Column(Text)
