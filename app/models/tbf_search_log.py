"""The Broker Forum (TBF) search log model.

Audit trail for every TBF search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.

Called by: tbf_worker worker loop, sighting_writer
Depends on: tbf_search_queue table
"""

from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Integer, String, Text

from ..database import UTCDateTime
from .base import Base


class TbfSearchLog(Base):
    __tablename__ = "tbf_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("tbf_search_queue.id", ondelete="CASCADE"), nullable=False, index=True)
    searched_at = Column(UTCDateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    duration_ms = Column(Integer)
    results_found = Column(Integer)
    sightings_created = Column(Integer)
    page_html_hash = Column(String(64))
    error = Column(Text)
