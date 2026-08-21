"""Search scheduler — thin TBF shim over search_worker_base.

The shared implementation lives in
app.services.search_worker_base.scheduler.SearchScheduler; this subclass
binds the "TBF" config-attribute prefix so existing imports and patch
targets (app.services.tbf_worker.scheduler.SearchScheduler) keep working.

Called by: worker loop
Depends on: search_worker_base.scheduler, config
"""

from ..search_worker_base.scheduler import SearchScheduler as _BaseSearchScheduler
from .config import TbfConfig


class SearchScheduler(_BaseSearchScheduler):
    """TBF search scheduler over the shared base implementation."""

    def __init__(self, config: TbfConfig):
        super().__init__(config, prefix="TBF")
