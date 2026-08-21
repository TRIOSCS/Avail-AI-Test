"""ICsource worker status model (singleton table).

Single-row table that the worker process updates and the API server
reads to display worker health on dashboards. Only one row (id=1)
is allowed via CHECK constraint. All columns and the singleton rule
come from WorkerStatusMixin (marketplace_search.py).

Called by: ics_worker.worker (heartbeat updates), ics_admin router (reads for
           the /api/ics/worker/health endpoint)
Depends on: nothing (standalone table)
"""

from .base import Base
from .marketplace_search import WorkerStatusMixin


class IcsWorkerStatus(WorkerStatusMixin, Base):
    __tablename__ = "ics_worker_status"
