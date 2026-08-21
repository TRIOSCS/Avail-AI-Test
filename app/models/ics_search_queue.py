"""ICsource search queue model.

Tracks parts that need to be searched on ICsource marketplace.
Each requirement with an MPN gets a queue entry; the AI gate decides
whether to search or skip based on commodity classification.
Columns/constraints are shared via SearchQueueMixin (marketplace_search.py).

Called by: ics_worker queue_manager, ai_gate, admin endpoints
Depends on: requirements, requisitions tables
"""

from .base import Base
from .marketplace_search import SearchQueueMixin


class IcsSearchQueue(SearchQueueMixin, Base):
    __tablename__ = "ics_search_queue"
