"""EBay search queue model.

Tracks parts that need to be searched on eBay's Browse API. Each requirement
with an MPN gets a queue entry; unlike the browser workers there is no AI
commodity gate — every queued MPN is searched and the spend is bounded by the
worker's daily call budget instead.
Columns/constraints are shared via SearchQueueMixin (marketplace_search.py).

Called by: ebay_worker queue_manager, worker loop, admin endpoints
Depends on: requirements, requisitions tables
"""

from .base import Base
from .marketplace_search import SearchQueueMixin


class EbaySearchQueue(SearchQueueMixin, Base):
    __tablename__ = "ebay_search_queue"
