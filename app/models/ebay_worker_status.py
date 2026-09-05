"""EBay worker status model (singleton table).

Single-row table that the eBay API-poller worker updates and the API server
reads to display worker health on the Connectors tab and the admin worker
snapshot. Only one row (id=1) is allowed via CHECK constraint. The shared
columns and the singleton rule come from WorkerStatusMixin
(marketplace_search.py).

Two columns are eBay-only and therefore declared here rather than on the
mixin: ``calls_today`` (Browse API calls spent) and ``budget_day`` (the UTC
date those calls belong to). The browser workers pace on business hours and
random breaks; the eBay poller instead spends a fixed daily call budget that
resets at midnight UTC, so it needs a durable per-UTC-day counter that
survives a worker restart.

Called by: ebay_worker.worker (heartbeat + budget updates), admin system
           router, Settings -> Connectors worker health
Depends on: nothing (standalone table)
"""

from sqlalchemy import Column, Date, Integer

from .base import Base
from .marketplace_search import WorkerStatusMixin


class EbayWorkerStatus(WorkerStatusMixin, Base):
    __tablename__ = "ebay_worker_status"

    # Browse API calls spent on `budget_day` (UTC). Reset to 0 when the worker
    # first ticks on a new UTC day.
    calls_today = Column(Integer, default=0)
    budget_day = Column(Date, nullable=True)
