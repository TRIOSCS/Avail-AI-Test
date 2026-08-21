"""NetComponents search log model.

Audit trail for every NC search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.
Shared measurement columns come from SearchLogMixin (marketplace_search.py).

Called by: nc_worker worker loop, sighting_writer
Depends on: nc_search_queue table
"""

from sqlalchemy import Column, ForeignKey, Integer

from .base import Base
from .marketplace_search import SearchLogMixin


class NcSearchLog(SearchLogMixin, Base):
    __tablename__ = "nc_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("nc_search_queue.id", ondelete="CASCADE"), nullable=False, index=True)
