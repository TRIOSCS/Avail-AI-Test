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
from zoneinfo import ZoneInfo

from loguru import logger

EASTERN = ZoneInfo("America/New_York")

# Track known HTML structure hashes per component
_known_html_hashes: dict[str, set[str]] = {}

# Cap the per-component hash set. After attributes are stripped (below) the number of
# genuinely-distinct tag structures a page can take is tiny (a handful), so this bound is
# only a safety net against pathological growth — it can never legitimately be reached in
# steady state. Before the strip, every per-row attribute value produced a "new" hash, so
# the set grew without bound and each new hash spammed a Sentry "layout changed" alert.
_MAX_STRUCTURE_HASHES = 64

# Matches an opening or closing tag's NAME only (``<td``, ``</td``, ``<custom-el``),
# deliberately stopping before any attributes. Tag names are a letter followed by
# letters/digits/hyphens (covers standard + custom elements). Doctype/comments start with
# ``<!`` and never match, so they're ignored.
_TAG_NAME_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*")


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

    The hash covers only the sequence of tag NAMES (open/close), never attributes:
    attribute values (``class``, ``id``, ``data-*``, inline ``style``) vary per row and
    per part, so folding them into the hash made almost every page look like a "layout
    change" — spamming Sentry and growing the stored hash set without bound. Genuine
    layout changes (a table becoming a list, a wrapper appearing) still alter the tag
    sequence and are still detected.
    """
    if not html:
        return ""

    hash_set = _get_hash_set(component_name)

    # Tag names only — attributes stripped (see docstring).
    structure = "".join(_TAG_NAME_RE.findall(html))
    struct_hash = hashlib.sha256(structure.encode()).hexdigest()[:16]

    if struct_hash not in hash_set:
        if hash_set:
            msg = f"{component_name} results HTML structure may have changed (hash={struct_hash}, mpn={queue_item_mpn})"
            logger.warning(msg)
            capture_sentry_message(
                msg,
                level="warning",
                context={"mpn": queue_item_mpn, "hash": struct_hash},
                component_name=component_name,
            )
        # Bound the set so it can never grow without limit (evict an arbitrary prior
        # hash at the cap). Only ever reachable if the strip above somehow still left
        # many distinct structures — a safety net, not a steady-state path.
        if len(hash_set) >= _MAX_STRUCTURE_HASHES:
            hash_set.pop()
        hash_set.add(struct_hash)
    return struct_hash
