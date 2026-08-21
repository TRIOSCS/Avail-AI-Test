"""ICsource search log model.

Audit trail for every ICS search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.
Shared measurement columns come from SearchLogMixin (marketplace_search.py).
The queue index is the named ix_ics_log_queue (historical — tbf/nc use the
column-level index=True naming instead).

Called by: ics_worker worker loop, sighting_writer
Depends on: ics_search_queue table
"""

from sqlalchemy import Column, ForeignKey, Index, Integer

from .base import Base
from .marketplace_search import SearchLogMixin


class IcsSearchLog(SearchLogMixin, Base):
    __tablename__ = "ics_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("ics_search_queue.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (Index("ix_ics_log_queue", "queue_id"),)
