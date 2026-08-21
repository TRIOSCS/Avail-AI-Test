"""The Broker Forum (TBF) search log model.

Audit trail for every TBF search attempt. Records timing, result counts,
and HTML structure hashes to detect layout changes.
Shared measurement columns come from SearchLogMixin (marketplace_search.py).

Called by: tbf_worker worker loop, sighting_writer
Depends on: tbf_search_queue table
"""

from sqlalchemy import Column, ForeignKey, Integer

from .base import Base
from .marketplace_search import SearchLogMixin


class TbfSearchLog(SearchLogMixin, Base):
    __tablename__ = "tbf_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("tbf_search_queue.id", ondelete="CASCADE"), nullable=False, index=True)
