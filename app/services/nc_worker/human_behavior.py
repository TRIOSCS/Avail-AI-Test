"""Human behavior simulation — re-exported from search_worker_base.

This module is a thin re-export for backward compatibility. The actual
implementation lives in app.services.search_worker_base.human_behavior.

Called by: session_manager (login flow)
Depends on: search_worker_base.human_behavior
"""

from ..search_worker_base.human_behavior import HumanBehavior

__all__ = ["HumanBehavior"]
