"""MPN normalizer — re-exported from search_worker_base.

This module is a thin re-export for backward compatibility. The actual
implementation lives in app.services.search_worker_base.mpn_normalizer.

Called by: queue_manager, sighting_writer
Depends on: search_worker_base.mpn_normalizer
"""

from ..search_worker_base.mpn_normalizer import normalize_mpn

__all__ = ["normalize_mpn"]
