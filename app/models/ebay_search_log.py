"""EBay search log model.

Audit trail for every eBay Browse API search attempt. Records timing and
result counts; ``page_html_hash`` (inherited from SearchLogMixin) carries the
hash of the JSON payload rather than HTML, so a silent response-shape change
is still detectable.

Called by: ebay_worker worker loop
Depends on: ebay_search_queue table
"""

from sqlalchemy import Column, ForeignKey, Integer

from .base import Base
from .marketplace_search import SearchLogMixin


class EbaySearchLog(SearchLogMixin, Base):
    __tablename__ = "ebay_search_log"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("ebay_search_queue.id", ondelete="CASCADE"), nullable=False, index=True)
