"""Health monitor service — scheduled API health checking.

Called by scheduler jobs to verify all active API connectors are reachable.
Two check levels:
  - ping: lightweight connectivity test via real API call (every 15 min)
  - deep: full search with known MPN + usage log (every 2 hours)

Status is determined ONLY by actual API responses, never by credential presence.
This is the source of truth for whether an API is actually working.

Depends on: app.models.config (ApiSource, ApiUsageLog), app.routers.sources (_get_connector_for_source)
Called by: app.scheduler (health_check_ping, health_check_deep jobs)
"""

import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models.config import ApiSource, ApiUsageLog

# Known good MPN for testing — universally available across all distributors
DEEP_TEST_MPN = "LM317"


def _get_connector(source: ApiSource, db: Session):
    """Get a connector instance for the given source. Returns None if unavailable."""
    from ..routers.sources import _get_connector_for_source

    try:
        return _get_connector_for_source(source.name, db)
    except Exception:
        return None


async def ping_source(source: ApiSource, db: Session) -> dict:
    """Lightweight health check — verify connector can be instantiated and respond.

    This is the ONLY place that should set source.status to 'live' or 'error'.
    Status is based on actual API response, not credential presence.

    Updates source status, last_ping_at, last_error fields.
    Returns dict with success, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
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
        db.flush()

        logger.warning("Health ping failed for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}


async def deep_test_source(source: ApiSource, db: Session) -> dict:
    """Full functional test — search a known MPN and verify results.

    Writes an ApiUsageLog entry for every test. Updates source timing fields.
    Returns dict with success, results_count, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
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
