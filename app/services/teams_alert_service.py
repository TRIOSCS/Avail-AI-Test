"""Teams DM alert service — hybrid Graph API + webhook delivery.

Sends targeted 1:1 Teams DMs to users at key workflow moments.
Primary delivery via Graph API (send_teams_dm), with per-user
webhook URL as fallback. Includes in-memory rate limiting and
audit logging to teams_notification_log.

Called by: email_service.py, routers/requisitions/core.py, routers/crm/offers.py, jobs/teams_alert_jobs.py
Depends on: app.services.teams_notifications, app.http_client, app.models
"""

import asyncio
import time
from collections import defaultdict

from loguru import logger

from app.http_client import http

# Rate limit: max 20 alerts per user per hour
_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW = 3600  # seconds
_rate_buckets: dict[int, list[float]] = defaultdict(list)


def _is_rate_limited(user_id: int) -> bool:
    """Check if a user has exceeded the alert rate limit."""
    now = time.time()
    bucket = _rate_buckets[user_id]
    # Prune expired entries
    _rate_buckets[user_id] = [t for t in bucket if now - t < _RATE_LIMIT_WINDOW]
    return len(_rate_buckets[user_id]) >= _RATE_LIMIT_MAX


def _mark_sent(user_id: int) -> None:
    """Record a send timestamp for rate limiting."""
    _rate_buckets[user_id].append(time.time())


def _log_alert(db, event_type: str, entity_id: str, success: bool, error_msg: str = None, user_id: int = None) -> None:
    """Write an audit entry to teams_notification_log."""
    try:
        from app.models.teams_notification_log import TeamsNotificationLog

        entry = TeamsNotificationLog(
            event_type=event_type,
            entity_id=entity_id,
            entity_name=f"user:{user_id}" if user_id else "",
            channel_id="dm",
            success=success,
            error_msg=error_msg,
        )
        db.add(entry)
        db.flush()
    except Exception:
        logger.debug("Failed to log alert audit entry", exc_info=True)


async def send_alert(db, user_id: int, message: str, event_type: str = "", entity_id: str = "") -> bool:
    """Send a Teams DM alert to a specific user with hybrid delivery.

    1. Load user + check config
    2. Try Graph API DM (send_teams_dm)
    3. If Graph fails: try webhook URL from TeamsAlertConfig
    4. Rate limit + audit log
    Returns True if delivered by either method.
    """
    from app.models.auth import User
    from app.models.teams_alert_config import TeamsAlertConfig

    # Check rate limit
    if _is_rate_limited(user_id):
        logger.debug("Rate limited alerts for user %d", user_id)
        return False

    # Load user
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return False

    # Check if alerts are enabled
    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user_id).first()
    if config and not config.alerts_enabled:
        return False

    # Intelligence gate — evaluate priority and decide on delivery
    ai_priority = ""
    ai_decision = "sent"
    try:
        if is_intelligence_enabled():
            decision = evaluate_dm_alert(user_id, event_type, entity_id, message, db=db)
            ai_priority = decision.priority
            ai_decision = decision.action.lower()

            if decision.action == "SUPPRESS":
                record_engagement(
                    user_id,
                    event_type,
                    entity_id,
                    "suppressed",
                    ai_priority=decision.priority,
                    suppression_reason=decision.reason,
                    db=db,
                )
                _log_alert(db, event_type, entity_id, True, user_id=user_id)
                return False

            if decision.action == "BATCH":
                queue_batch_alert(user_id, event_type, entity_id, message, decision.priority)
                record_engagement(
                    user_id,
                    event_type,
                    entity_id,
                    "batched",
                    ai_priority=decision.priority,
                    db=db,
                )
                return False
    except Exception:
        logger.debug("Intelligence gate error, proceeding with delivery", exc_info=True)

    # Try Graph API DM first
    graph_ok = await _try_graph_dm(user, message, db)
    if graph_ok:
        _mark_sent(user_id)
        _log_alert(db, event_type, entity_id, True, user_id=user_id)
        try:
            if is_intelligence_enabled():
                record_engagement(user_id, event_type, entity_id, "delivered", ai_priority=ai_priority, db=db)
        except Exception:
            logger.debug("Failed to record engagement for alert %s:%s", event_type, entity_id, exc_info=True)
        return True

    # Fallback: webhook URL
    if config and config.teams_webhook_url:
        webhook_ok = await _try_webhook(config.teams_webhook_url, message)
        if webhook_ok:
            _mark_sent(user_id)
            _log_alert(db, event_type, entity_id, True, user_id=user_id)
            try:
                if is_intelligence_enabled():
                    record_engagement(
                        user_id,
                        event_type,
                        entity_id,
                        "delivered",
                        delivery_method="webhook",
                        ai_priority=ai_priority,
                        db=db,
                    )
            except Exception:
                logger.debug(
                    "Failed to record webhook engagement for alert %s:%s", event_type, entity_id, exc_info=True
                )
            return True

    _log_alert(db, event_type, entity_id, False, "No delivery method available", user_id=user_id)
    return False


async def _try_graph_dm(user, message: str, db) -> bool:
    """Attempt Graph API DM delivery. Returns True on success."""
    try:
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, message, db)
        # send_teams_dm doesn't return success/failure — check if user has token
        if user.access_token:
            return True
        return False
    except Exception as e:
        logger.debug("Graph DM failed for %s: %s", user.email, e)
        return False


async def _try_webhook(webhook_url: str, message: str) -> bool:
    """POST plain text to a per-user webhook. Retry once on 5xx."""
    for attempt in range(2):
        try:
            resp = await http.post(webhook_url, json={"text": message}, timeout=10)
            if resp.status_code in (200, 202):
                return True
            if resp.status_code >= 500 and attempt == 0:
                await asyncio.sleep(2)
                continue
            logger.warning("Webhook returned %d", resp.status_code)
            return False
        except Exception as e:
            logger.debug("Webhook delivery failed: %s", e)
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            return False
    return False


async def send_alert_to_role(
    db, role: str, message: str, event_type: str = "", entity_id: str = "", exclude_user_id: int = None
) -> int:
    """Send alert to all active users with given role who have alerts enabled.

    Returns count of successfully delivered alerts.
    """
    from app.models.auth import User

    users = db.query(User).filter(User.role == role, User.is_active.is_(True)).all()
    sent = 0
    for user in users:
        if exclude_user_id and user.id == exclude_user_id:
            continue
        ok = await send_alert(db, user.id, message, event_type, entity_id)
        if ok:
            sent += 1
    return sent


def _resolve_director_id(db) -> int | None:
    """Auto-detect first active user with role='manager'."""
    from app.models.auth import User

    mgr = db.query(User).filter(User.role == "manager", User.is_active.is_(True)).first()
    return mgr.id if mgr else None
