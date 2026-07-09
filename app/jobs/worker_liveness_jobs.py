"""Worker liveness watchdog — alerts when a background worker stops heartbeating.

Each worker (ICS, NetComponents, The Broker Forum, enrichment) writes
``last_heartbeat`` on every loop tick. systemd/docker restart a *crashed* worker,
but a *hung* one (wedged browser, deadlock, network stall) keeps its process alive
while doing nothing — nobody notices. This job reads the heartbeats every few
minutes and alerts (Teams + Sentry + logs, debounced per worker) when a worker that
should be running has gone silent, or has its circuit breaker open.

The staleness + debounce decision is factored into pure functions
(``heartbeat_is_stale`` / ``should_alert_stale_heartbeat``) so it is unit-testable
without the scheduler or a database.

Called by: app/jobs/__init__.py via register_worker_liveness_jobs().
Depends on: worker_status models, services.teams_notifications, cache.intel_cache
            (debounce store — Redis-backed, no schema/column), search_worker_base.monitoring (Sentry).
"""

from datetime import UTC, datetime, timedelta

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
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── Pure decision logic (no DB / scheduler / IO — unit-testable) ─────────


def heartbeat_is_stale(
    is_running: bool,
    last_heartbeat: datetime | None,
    now: datetime,
    stale_after_minutes: int,
) -> bool:
    """Return True when a worker that claims to be running has gone silent.

    A NULL/never-seen heartbeat counts as stale. A worker with ``is_running``
    False is never stale — a clean shutdown sets that flag, so expected silence
    does not false-alarm. Naive timestamps are coerced to UTC before comparison.
    """
    if not is_running:
        return False
    if last_heartbeat is None:
        return True
    return _as_utc(last_heartbeat) < now - timedelta(minutes=stale_after_minutes)


def should_alert_stale_heartbeat(
    *,
    is_running: bool,
    last_heartbeat: datetime | None,
    now: datetime,
    stale_after_minutes: int,
    already_alerted: bool,
) -> bool:
    """Pure staleness + debounce gate the watchdog uses to decide whether to emit.

    Emits only when the worker's heartbeat is stale (see ``heartbeat_is_stale``)
    AND we have not already alerted within the debounce window
    (``already_alerted`` is False).
    """
    return heartbeat_is_stale(is_running, last_heartbeat, now, stale_after_minutes) and not already_alerted


# ── Debounce store + alert fan-out ──────────────────────────────────────


def _already_alerted(label: str) -> bool:
    """Debounce read: True while an alert for ``label`` is still within its window.

    State lives in the Redis-backed intel_cache (TTL = debounce window), so no
    new column/migration is needed and the debounce is shared across processes.
    """
    from ..cache.intel_cache import get_cached

    return get_cached(f"worker_alert:{label}") is not None


async def _emit_alert(label: str, message: str, debounce_minutes: int) -> None:
    """Record the debounce marker, then fan the alert out to logs, Sentry, Teams."""
    from ..cache.intel_cache import set_cached

    set_cached(f"worker_alert:{label}", {"alerted": 1}, ttl_days=debounce_minutes / 1440)

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
    from ..config import settings
    from ..database import SessionLocal
    from ..models import EnrichmentWorkerStatus, IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus

    now = datetime.now(UTC)
    stale_minutes = settings.worker_heartbeat_stale_minutes
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
            is_running = bool(row.is_running)  # type: ignore[attr-defined]  # dynamic per-worker status model
            hb = row.last_heartbeat  # type: ignore[attr-defined]  # dynamic per-worker status model

            # Stale: claims to be running but the heartbeat went silent (or never landed).
            if should_alert_stale_heartbeat(
                is_running=is_running,
                last_heartbeat=hb,
                now=now,
                stale_after_minutes=stale_minutes,
                already_alerted=_already_alerted(label),
            ):
                hb_utc = _as_utc(hb)
                age = "unknown" if hb_utc is None else f"{int((now - hb_utc).total_seconds() // 60)}m"
                await _emit_alert(
                    label,
                    f"{label} worker heartbeat is stale (last seen {age} ago). "
                    "It may be hung or crashed — check the service.",
                    debounce,
                )
            # Up but not working: circuit breaker open. Only when not stale — a silent
            # worker's headline problem is the silence, so we don't double-alert.
            elif not heartbeat_is_stale(is_running, hb, now, stale_minutes) and getattr(
                row, "circuit_breaker_open", False
            ):
                if not _already_alerted(f"{label}-breaker"):
                    reason = getattr(row, "circuit_breaker_reason", None) or "unknown"
                    await _emit_alert(
                        f"{label}-breaker",
                        f"{label} worker circuit breaker is OPEN: {reason}",
                        debounce,
                    )
    finally:
        db.close()
