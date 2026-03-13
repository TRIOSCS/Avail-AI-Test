"""Health monitor service — scheduled API health checking + proactive alerts.

Called by scheduler jobs to verify all active API connectors are reachable.
Two check levels:
  - ping: lightweight connectivity test via real API call (every 15 min)
  - deep: full search with known MPN + usage log (every 2 hours)

Status is determined ONLY by actual API responses, never by credential presence.
This is the source of truth for whether an API is actually working.

Proactive alerts:
  - Notifies admin users when a source transitions live → error
  - Warns when quota usage exceeds 80% or 95% of monthly_quota

Depends on: app.models.config (ApiSource, ApiUsageLog), app.routers.sources (_get_connector_for_source)
Called by: app.scheduler (health_check_ping, health_check_deep jobs)
"""

import re
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models.config import ApiSource, ApiUsageLog

# Quota warning thresholds (percentage of monthly_quota)
QUOTA_WARN_THRESHOLD = 80
QUOTA_CRITICAL_THRESHOLD = 95

# Regex patterns for common API key formats in error messages/URLs
_API_KEY_RE = re.compile(
    r"""(?x)
    (?:api[_-]?key|apikey|token|secret|authorization|bearer|key|password|passwd)
    \s*[=:]\s*
    (['"]?)([A-Za-z0-9\-_./+]{8,})(\1)
    """,
    re.IGNORECASE,
)
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9\-_]{20,}")


def _redact_api_keys(text: str | None) -> str | None:
    """Redact potential API keys from notification text to prevent key leakage."""
    if not text:
        return text

    # Redact named key patterns (api_key=xxx, token=xxx, etc.)
    def _mask_named(m):
        prefix = m.group(0)[: m.start(2) - m.start(0)]
        key = m.group(2)
        quote = m.group(1) or ""
        if len(key) <= 4:
            return m.group(0)
        masked = key[:3] + "***" + key[-3:]
        return f"{prefix}{quote}{masked}{quote}"

    result = _API_KEY_RE.sub(_mask_named, text)

    # Redact long bare tokens that look like API keys in URLs
    def _mask_bare(m):
        key = m.group(0)
        # Skip things that are clearly not keys (common words, hex hashes, etc.)
        if len(key) > 100:
            return key
        return key[:3] + "***" + key[-3:]

    # Only apply bare key masking to URL query parameters
    if "?" in result or "&" in result:
        parts = result.split("?", 1)
        if len(parts) == 2:
            parts[1] = _BARE_KEY_RE.sub(_mask_bare, parts[1])
            result = "?".join(parts)
    return result


# Known good MPN for testing — universally available across all distributors
DEEP_TEST_MPN = "LM317"


def _get_connector(source: ApiSource, db: Session):
    """Get a connector instance for the given source. Returns None if unavailable."""
    from ..routers.sources import _get_connector_for_source

    try:
        return _get_connector_for_source(source.name, db)
    except Exception:
        return None


def _notify_admins(db: Session, event_type: str, title: str, body: str | None = None):
    """Log an admin-level health alert (notification_service removed)."""
    message = f"[{event_type}] {title}" + (f" — {body}" if body else "")
    logger.warning("API source health alert: {}", message)


def _check_status_transition(
    source: ApiSource, old_status: str, new_status: str, db: Session, error_msg: str | None = None
):
    """Fire notification when a source transitions from live to error."""
    if old_status == "live" and new_status == "error":
        safe_error = _redact_api_keys(error_msg) if error_msg else "unknown"
        _notify_admins(
            db,
            event_type="api_source_down",
            title=f"API down: {source.display_name or source.name}",
            body=f"{source.display_name or source.name} changed from live → error. Last error: {safe_error}",
        )
        logger.warning("Source {} transitioned live → error, admin notification sent", source.name)


def _check_quota_threshold(source: ApiSource, db: Session):
    """Warn admins when quota usage crosses 80% or 95%."""
    if not source.monthly_quota or source.monthly_quota <= 0:
        return
    usage_pct = ((source.calls_this_month or 0) / source.monthly_quota) * 100
    if usage_pct >= QUOTA_CRITICAL_THRESHOLD:
        _notify_admins(
            db,
            event_type="api_quota_critical",
            title=f"Quota critical: {source.display_name or source.name} ({usage_pct:.0f}%)",
            body=f"{source.calls_this_month}/{source.monthly_quota} calls used this month. Service may stop working soon.",
        )
    elif usage_pct >= QUOTA_WARN_THRESHOLD:
        _notify_admins(
            db,
            event_type="api_quota_warning",
            title=f"Quota warning: {source.display_name or source.name} ({usage_pct:.0f}%)",
            body=f"{source.calls_this_month}/{source.monthly_quota} calls used this month.",
        )


async def ping_source(source: ApiSource, db: Session) -> dict:
    """Lightweight health check — verify connector can be instantiated and respond.

    This is the ONLY place that should set source.status to 'live' or 'error'.
    Status is based on actual API response, not credential presence.

    Updates source status, last_ping_at, last_error fields.
    Returns dict with success, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
    old_status = source.status
    connector = _get_connector(source, db)

    if not connector:
        source.status = "disabled"
        source.last_ping_at = now
        db.flush()
        return {"success": False, "error": "No connector available", "elapsed_ms": 0}

    start = time.time()
    try:
        await connector.search(DEEP_TEST_MPN)
        elapsed_ms = int((time.time() - start) * 1000)

        source.status = "live"
        source.last_success = now
        source.last_ping_at = now
        source.last_error = None
        source.avg_response_ms = elapsed_ms
        source.calls_this_month = (source.calls_this_month or 0) + 1
        _check_quota_threshold(source, db)

        # Log ping to ApiUsageLog so dashboard recent_checks reflects all checks
        log = ApiUsageLog(
            source_id=source.id,
            timestamp=now,
            endpoint="ping",
            status_code=200,
            response_ms=elapsed_ms,
            success=True,
            check_type="ping",
        )
        db.add(log)
        db.flush()

        return {"success": True, "elapsed_ms": elapsed_ms, "error": None}

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = str(e)[:500]

        source.status = "error"
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)

        # Log failed ping to ApiUsageLog
        log = ApiUsageLog(
            source_id=source.id,
            timestamp=now,
            endpoint="ping",
            response_ms=elapsed_ms,
            success=False,
            error_message=error_msg,
            check_type="ping",
        )
        db.add(log)
        db.flush()

        logger.warning("Health ping failed for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}


async def deep_test_source(source: ApiSource, db: Session) -> dict:
    """Full functional test — search a known MPN and verify results.

    Writes an ApiUsageLog entry for every test. Updates source timing fields.
    Returns dict with success, results_count, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
    old_status = source.status
    connector = _get_connector(source, db)

    if not connector:
        log = ApiUsageLog(
            source_id=source.id,
            timestamp=now,
            endpoint="deep_test",
            success=False,
            error_message="No connector",
            check_type="deep",
        )
        db.add(log)
        source.status = "disabled"
        source.last_deep_test_at = now
        db.flush()
        return {"success": False, "results_count": 0, "elapsed_ms": 0, "error": "No connector"}

    start = time.time()
    try:
        results = await connector.search(DEEP_TEST_MPN)
        elapsed_ms = int((time.time() - start) * 1000)

        source.status = "live"
        source.last_success = now
        source.last_deep_test_at = now
        source.last_error = None
        source.avg_response_ms = elapsed_ms
        source.calls_this_month = (source.calls_this_month or 0) + 1
        _check_quota_threshold(source, db)

        log = ApiUsageLog(
            source_id=source.id,
            timestamp=now,
            endpoint="deep_test",
            status_code=200,
            response_ms=elapsed_ms,
            success=True,
            check_type="deep",
        )
        db.add(log)
        db.flush()

        return {"success": True, "results_count": len(results), "elapsed_ms": elapsed_ms, "error": None}

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = str(e)[:500]

        source.status = "error"
        source.last_error = error_msg
        source.last_error_at = now
        source.last_deep_test_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)

        log = ApiUsageLog(
            source_id=source.id,
            timestamp=now,
            endpoint="deep_test",
            response_ms=elapsed_ms,
            success=False,
            error_message=error_msg,
            check_type="deep",
        )
        db.add(log)
        db.flush()

        logger.warning("Deep test failed for {}: {}", source.name, error_msg)
        return {"success": False, "results_count": 0, "elapsed_ms": elapsed_ms, "error": error_msg}


async def run_health_checks(check_type: str = "ping") -> dict:
    """Run health checks on all active sources.

    Args:
        check_type: "ping" for lightweight, "deep" for full functional test.

    Returns dict with total, passed, failed counts and per-source results.
    """
    from ..database import SessionLocal

    db = SessionLocal()
    results = {"total": 0, "passed": 0, "failed": 0, "sources": {}}

    try:
        sources = (
            db.query(ApiSource)
            .filter(ApiSource.is_active == True)  # noqa: E712
            .all()
        )
        results["total"] = len(sources)

        check_fn = deep_test_source if check_type == "deep" else ping_source

        for source in sources:
            try:
                result = await check_fn(source, db)
                results["sources"][source.name] = result
                if result["success"]:
                    results["passed"] += 1
                else:
                    results["failed"] += 1
            except Exception as e:
                logger.error("Health check crashed for {}: {}", source.name, e)
                results["sources"][source.name] = {"success": False, "error": str(e)}
                results["failed"] += 1

        db.commit()
        logger.info(
            "Health check ({}) complete: {}/{} passed",
            check_type,
            results["passed"],
            results["total"],
        )

    except Exception as e:
        logger.exception("Health check run failed: {}", e)
        db.rollback()
    finally:
        db.close()

    return results
