"""Worker liveness watchdog — alerts when a background worker stops heartbeating.

Each worker (ICS, NetComponents, enrichment) writes ``last_heartbeat`` on every loop
tick. systemd/docker restart a *crashed* worker, but a *hung* one (wedged browser,
deadlock, network stall) keeps its process alive while doing nothing — nobody notices.
This job reads the heartbeats every few minutes and alerts (Teams + Sentry, debounced)
when a worker that should be running has gone silent, or has its circuit breaker open.

Called by: app/jobs/__init__.py via register_worker_liveness_jobs().
Depends on: worker_status models, services.teams_notifications, cache.intel_cache (debounce),
            search_worker_base.monitoring (Sentry).
"""

from datetime import datetime, timezone

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_worker_liveness_jobs(scheduler, settings):
    scheduler.add_job(
        _job_monitor_worker_heartbeats,
        IntervalTrigger(minutes=settings.worker_liveness_check_minutes),
        id="worker_liveness_check",
        name="Monitor worker heartbeats and alert on stalls",
    )


def _as_utc(dt):
    """Coerce a (possibly naive) datetime to UTC-aware — ICS stores naive, NC aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _alert(label: str, message: str, debounce_minutes: int) -> None:
    """Debounced alert (1× per label per window) to Teams + Sentry + logs."""
    from ..cache.intel_cache import get_cached, set_cached

    key = f"worker_alert:{label}"
    if get_cached(key) is not None:
        return  # already alerted within the debounce window
    set_cached(key, {"alerted": 1}, ttl_days=debounce_minutes / 1440)

    logger.error("WORKER WATCHDOG: {}", message)
    try:
        from ..services.search_worker_base.monitoring import capture_sentry_message

        capture_sentry_message(message, level="error", component_name="worker_watchdog")
    except Exception:  # pragma: no cover - Sentry optional
        pass
    try:
        from ..services.teams_notifications import post_teams_channel

        await post_teams_channel(f"⚠️ **Worker alert — {label}**\n\n{message}")
    except Exception as e:
        logger.warning("Worker watchdog Teams alert failed: {}", e)


@_traced_job
async def _job_monitor_worker_heartbeats():
    from datetime import timedelta

    from ..config import settings
    from ..database import SessionLocal
    from ..models import EnrichmentWorkerStatus, IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=settings.worker_heartbeat_stale_minutes)
    debounce = settings.worker_alert_debounce_minutes

    checks = (
        ("ICS", IcsWorkerStatus),
        ("NetComponents", NcWorkerStatus),
        ("The Broker Forum", TbfWorkerStatus),
        ("Enrichment", EnrichmentWorkerStatus),
    )
    db = SessionLocal()
    try:
        for label, model in checks:
            row = db.get(model, 1)
            if row is None:
                continue
            hb = _as_utc(row.last_heartbeat)
            # Stale: claims to be running but the heartbeat went silent. (A clean
            # shutdown sets is_running=False, so this won't false-alarm on a stop.)
            if row.is_running and (hb is None or hb < stale_cutoff):
                age = "unknown" if hb is None else f"{int((now - hb).total_seconds() // 60)}m"
                await _alert(
                    label,
                    f"{label} worker heartbeat is stale (last seen {age} ago). "
                    f"It may be hung or crashed — check the service.",
                    debounce,
                )
            # Up but not working: circuit breaker open.
            elif getattr(row, "circuit_breaker_open", False):
                reason = getattr(row, "circuit_breaker_reason", None) or "unknown"
                await _alert(
                    f"{label}-breaker",
                    f"{label} worker circuit breaker is OPEN: {reason}",
                    debounce,
                )
    finally:
        db.close()
