"""Search worker monitoring — daily reports, Sentry alerts, HTML hash tracking.

Provides daily summary logging, Sentry error capture for circuit breaker
trips and crashes, and HTML structure hash monitoring to detect layout changes.
Parameterized by component_name (e.g. "ICS", "NC") so both workers share one implementation.

Called by: worker loop
Depends on: sentry_sdk, loguru
"""

import hashlib
import re
from contextlib import contextmanager
from datetime import datetime

from loguru import logger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pragma: no cover

EASTERN = ZoneInfo("America/New_York")

# Track known HTML structure hashes per component
_known_html_hashes: dict[str, set[str]] = {}


def _get_hash_set(component_name: str) -> set[str]:
    """Return the hash set for a given component, creating it if needed."""
    return _known_html_hashes.setdefault(component_name, set())


def log_daily_report(
    searches_completed: int,
    sightings_created: int,
    parts_gated_out: int,
    parts_deduped: int,
    failed_searches: int,
    queue_remaining: int,
    circuit_breaker_status: str,
    component_name: str = "Worker",
):
    """Log the end-of-day summary report."""
    date_str = datetime.now(EASTERN).strftime("%b %d, %Y")
    report = f"""
{component_name} Worker Daily Report — {date_str}
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


@contextmanager
def _sentry_scope(component_name: str, context: dict | None):
    """Yield a Sentry scope tagged with the worker component and extra context.

    Raises ImportError if the Sentry SDK is not installed — callers handle it so the
    missing-SDK log message can stay specific to what was being sent.
    """
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", f"{component_name.lower()}_worker")
        for key, value in (context or {}).items():
            scope.set_extra(key, value)
        yield sentry_sdk


def capture_sentry_error(error: Exception, context: dict | None = None, component_name: str = "worker"):
    """Send an error to Sentry with worker context."""
    try:
        with _sentry_scope(component_name, context) as sentry_sdk:
            sentry_sdk.capture_exception(error)
    except ImportError:
        logger.warning("Sentry SDK not available, logging error only: {}", error)


def capture_sentry_message(
    message: str, level: str = "warning", context: dict | None = None, component_name: str = "worker"
):
    """Send a message to Sentry with worker context."""
    try:
        with _sentry_scope(component_name, context) as sentry_sdk:
            sentry_sdk.capture_message(message, level=level)
    except ImportError:
        logger.warning("Sentry SDK not available: {}", message)


def check_html_structure_hash(html: str, queue_item_mpn: str, component_name: str = "Worker") -> str:
    """Compute a hash of the HTML tag structure (not content) to detect layout changes.

    Returns the structure hash. Logs a warning if the structure is new.
    """
    if not html:
        return ""

    hash_set = _get_hash_set(component_name)

    # Extract just the tag structure: <tag attr>...<tag> pattern
    tags = re.findall(r"</?[a-zA-Z][^>]*>", html)
    structure = "".join(tags)
    struct_hash = hashlib.sha256(structure.encode()).hexdigest()[:16]

    if hash_set and struct_hash not in hash_set:
        msg = f"{component_name} results HTML structure may have changed (hash={struct_hash}, mpn={queue_item_mpn})"
        logger.warning(msg)
        capture_sentry_message(
            msg, level="warning", context={"mpn": queue_item_mpn, "hash": struct_hash}, component_name=component_name
        )

    hash_set.add(struct_hash)
    return struct_hash
