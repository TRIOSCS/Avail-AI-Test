"""ICS worker monitoring — thin wrapper around search_worker_base.monitoring.

Re-exports all monitoring functions with component_name="ICS" pre-applied.

Called by: worker loop, tests
Depends on: app.services.search_worker_base.monitoring
"""

from functools import partial

from ..search_worker_base.monitoring import (
    _get_hash_set,
    _known_html_hashes,
)
from ..search_worker_base.monitoring import (
    capture_sentry_error as _capture_error,
)
from ..search_worker_base.monitoring import (
    capture_sentry_message as _capture_message,
)
from ..search_worker_base.monitoring import (
    check_html_structure_hash as _check_hash,
)
from ..search_worker_base.monitoring import (
    log_daily_report as _log_report,
)

# Re-export with ICS-specific defaults
log_daily_report = partial(_log_report, component_name="ICS")
capture_sentry_error = partial(_capture_error, component_name="ics")
capture_sentry_message = partial(_capture_message, component_name="ics")
check_html_structure_hash = partial(_check_hash, component_name="ICS")

__all__ = [
    "_get_hash_set",
    "_known_html_hashes",
    "capture_sentry_error",
    "capture_sentry_message",
    "check_html_structure_hash",
    "log_daily_report",
]
