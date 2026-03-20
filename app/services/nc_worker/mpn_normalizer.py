"""MPN normalizer for NC worker — delegates to shared base.

Called by: nc_worker modules
Depends on: search_worker_base.mpn_normalizer
"""

from ..search_worker_base.mpn_normalizer import strip_packaging_suffixes

__all__ = ["strip_packaging_suffixes"]
