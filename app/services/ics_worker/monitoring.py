"""ICS worker monitoring — daily reports, Sentry alerts, HTML hash tracking.

Provides daily summary logging, Sentry error capture for circuit breaker
trips and crashes, and HTML structure hash monitoring to detect layout changes.

Called by: worker loop
Depends on: sentry_sdk, loguru, ics_search_log model
"""

import hashlib
import re
from datetime import datetime

from loguru import logger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pragma: no cover

EASTERN = ZoneInfo("America/New_York")

# Track known HTML structure hashes
_known_html_hashes: set[str] = set()


def log_daily_report(
    searches_completed: int,
    sightings_created: int,
    parts_gated_out: int,
    parts_deduped: int,
    failed_searches: int,
    queue_remaining: int,
    circuit_breaker_status: str,
):
    """Log the end-of-day summary report."""
    date_str = datetime.now(EASTERN).strftime("%b %d, %Y")
    report = f"""
ICS Worker Daily Report — {date_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Searches completed:  {searches_completed}
Sightings created:   {sightings_created}
Parts gated out:     {parts_gated_out}
Parts deduped:       {parts_deduped}
Failed searches:     {failed_searches}
Queue remaining:     {queue_remaining}
Circuit breaker:     {circuit_breaker_status}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    logger.info(report)


def capture_sentry_error(error: Exception, context: dict | None = None):
    """Send an error to Sentry with ICS worker context."""
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "ics_worker")
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_exception(error)
    except ImportError:
        logger.warning("Sentry SDK not available, logging error only: {}", error)


def capture_sentry_message(message: str, level: str = "warning", context: dict | None = None):
    """Send a message to Sentry with ICS worker context."""
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "ics_worker")
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level=level)
    except ImportError:
        logger.warning("Sentry SDK not available: {}", message)


def check_html_structure_hash(html: str, queue_item_mpn: str) -> str:
    """Compute a hash of the HTML tag structure (not content) to detect layout changes.

    Returns the structure hash. Logs a warning if the structure is new.
    """
    if not html:
        return ""

    # Extract just the tag structure: <tag attr>...<tag> pattern
    tags = re.findall(r"</?[a-zA-Z][^>]*>", html)
    structure = "".join(tags)
    struct_hash = hashlib.sha256(structure.encode()).hexdigest()[:16]

    if _known_html_hashes and struct_hash not in _known_html_hashes:
        msg = f"ICS results HTML structure may have changed (hash={struct_hash}, mpn={queue_item_mpn})"
        logger.warning(msg)
        capture_sentry_message(msg, level="warning", context={"mpn": queue_item_mpn, "hash": struct_hash})

    _known_html_hashes.add(struct_hash)
    return struct_hash
