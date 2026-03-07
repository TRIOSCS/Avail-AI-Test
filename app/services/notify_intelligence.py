"""Notification intelligence service — AI-driven alert prioritization and noise reduction.

Evaluates alerts using a two-stage classification pipeline:
  Stage 1: Rule-based priority matrix (90% of alerts, zero latency)
  Stage 2: Claude Haiku for ambiguous cases (~10%, ~$0.35/month)

5-tier priority: critical > high > medium > low > noise
3 decisions: SEND_NOW, BATCH, SUPPRESS

Called by: app/services/teams.py, app/services/teams_alert_service.py
Depends on: app/models/notification_engagement.py, app/utils/claude_client.py, Redis
"""

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

# Priority levels ordered by severity
PRIORITY_LEVELS = ["noise", "low", "medium", "high", "critical"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITY_LEVELS)}


@dataclass
class AlertDecision:
    action: str  # SEND_NOW, BATCH, SUPPRESS
    priority: str  # critical, high, medium, low, noise
    confidence: float = 1.0
    reason: str = ""
    batch_key: str = ""


# Rule-based priority matrix: event_type -> base priority
EVENT_PRIORITY_MAP = {
    # Critical
    "connector_down": "critical",
    "buyplan_escalation": "critical",
    "pipeline_milestone": "critical",
    # High
    "hot_requirement": "high",
    "competitive_quote": "high",
    "buyplan_submitted": "high",
    "buyplan_approved": "high",
    "buyplan_rejected": "high",
    # Medium
    "ownership_expiring": "medium",
    "stock_match": "medium",
    "price_drop": "medium",
    "trouble_ticket_opened": "medium",
    "trouble_ticket_resolved": "medium",
    # Low
    "buyplan_completed": "low",
    "buyplan_cancelled": "low",
    "weekly_digest": "low",
    "morning_briefing": "low",
    "director_digest": "low",
}


def _get_redis():
    """Get Redis client. Returns None if unavailable."""
    if os.environ.get("TESTING"):
        return None
    try:
        from app.cache.intel_cache import _get_redis as _cache_get_redis
        return _cache_get_redis()
    except Exception:
        return None


def _classify_priority(event_type: str, context: dict | None = None) -> tuple[str, float]:
    """Stage 1: Rule-based priority classification.

    Returns (priority, confidence). High confidence means no need for AI.
    """
    ctx = context or {}
    base = EVENT_PRIORITY_MAP.get(event_type, "medium")

    # Value-based upgrades for hot_requirement
    if event_type == "hot_requirement":
        total_value = ctx.get("total_value", 0)
        if total_value >= 50_000:
            return "critical", 1.0
        elif total_value >= 10_000:
            return "high", 0.95
        return "medium", 0.9

    # Undercut severity for competitive_quote
    if event_type == "competitive_quote":
        savings_pct = ctx.get("savings_pct", 0)
        if savings_pct >= 30:
            return "critical", 0.95
        elif savings_pct >= 20:
            return "high", 0.9
        return "medium", 0.85

    # Pipeline milestones — won is always critical
    if event_type == "pipeline_milestone":
        if ctx.get("status", "").lower() == "won":
            return "critical", 1.0
        return "high", 0.95

    return base, 0.9


def _check_staleness(event_type: str, entity_id: str) -> bool:
    """Check if same entity has 3+ alerts in 24h with no engagement.

    Returns True if the alert should be suppressed as noise.
    """
    r = _get_redis()
    if not r:
        return False

    key = f"notif_stale:{event_type}:{entity_id}"
    try:
        count = r.get(key)
        if count and int(count) >= 3:
            return True
        # Increment counter with 24h TTL
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 86400)
        pipe.execute()
    except Exception:
        pass
    return False


def _check_quiet_hours(user_id: int, db) -> bool:
    """Check if user is in quiet hours. Returns True if in quiet hours."""
    try:
        from app.models.teams_alert_config import TeamsAlertConfig
        config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user_id).first()
        if not config or not config.quiet_hours_start or not config.quiet_hours_end:
            return False

        now = datetime.now(timezone.utc).time()
        start = config.quiet_hours_start
        end = config.quiet_hours_end

        # Handle overnight quiet hours (e.g., 22:00 to 06:00)
        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end
    except Exception:
        return False


def _get_user_threshold(user_id: int, db) -> str:
    """Get user's minimum priority threshold for DMs."""
    try:
        from app.models.teams_alert_config import TeamsAlertConfig
        config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user_id).first()
        if config:
            return config.priority_threshold or "medium"
    except Exception:
        pass
    return "medium"


def _get_engagement_dismissal_rate(user_id: int, event_type: str, db) -> float:
    """Get 30-day dismissal rate for user+event_type.

    Returns fraction of dismissed vs total (0.0 to 1.0).
    """
    # Check Redis cache first
    r = _get_redis()
    cache_key = f"notif_dismiss_rate:{user_id}:{event_type}"
    if r:
        try:
            cached = r.get(cache_key)
            if cached is not None:
                return float(cached)
        except Exception:
            pass

    try:
        from sqlalchemy import func
        from app.models.notification_engagement import NotificationEngagement

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        total = (
            db.query(func.count(NotificationEngagement.id))
            .filter(
                NotificationEngagement.user_id == user_id,
                NotificationEngagement.event_type == event_type,
                NotificationEngagement.created_at > cutoff,
            )
            .scalar()
        ) or 0

        if total < 5:  # Not enough data
            return 0.0

        dismissed = (
            db.query(func.count(NotificationEngagement.id))
            .filter(
                NotificationEngagement.user_id == user_id,
                NotificationEngagement.event_type == event_type,
                NotificationEngagement.action == "dismissed",
                NotificationEngagement.created_at > cutoff,
            )
            .scalar()
        ) or 0

        rate = dismissed / total

        # Cache for 15 minutes
        if r:
            try:
                r.setex(cache_key, 900, str(round(rate, 3)))
            except Exception:
                pass

        return rate
    except Exception:
        return 0.0


async def _ai_classify(event_type: str, entity_id: str, message: str, context: dict | None = None) -> AlertDecision | None:
    """Stage 2: Claude Haiku classification for ambiguous cases."""
    try:
        from app.utils.claude_client import claude_structured

        schema = {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low", "noise"]},
                "reason": {"type": "string"},
                "suggested_action": {"type": "string"},
            },
            "required": ["priority", "reason"],
        }

        result = await claude_structured(
            prompt=(
                f"Classify this business notification priority.\n"
                f"Event: {event_type}\n"
                f"Entity: {entity_id}\n"
                f"Message: {message[:500]}\n"
                f"Context: {str(context or {})[:300]}"
            ),
            schema=schema,
            system=(
                "You classify business notifications for an electronic component sourcing platform. "
                "critical=needs immediate action, high=important today, medium=useful info, "
                "low=can batch into digest, noise=suppress entirely."
            ),
            model_tier="fast",
            max_tokens=200,
        )

        if result:
            priority = result.get("priority", "medium")
            action = "SEND_NOW" if priority in ("critical", "high") else ("BATCH" if priority in ("medium", "low") else "SUPPRESS")
            return AlertDecision(
                action=action,
                priority=priority,
                confidence=0.85,
                reason=result.get("reason", "AI classified"),
            )
    except Exception:
        logger.debug("AI classification failed, falling back to rules", exc_info=True)
    return None


def evaluate_channel_alert(event_type: str, entity_id: str, context: dict | None = None) -> AlertDecision:
    """Evaluate whether a channel alert should be sent, batched, or suppressed.

    This replaces _is_rate_limited() calls in teams.py for channel posts.
    Falls through to SEND_NOW on any error (fire-and-forget safety).
    """
    try:
        # Check staleness (3+ alerts for same entity in 24h)
        if _check_staleness(event_type, str(entity_id)):
            return AlertDecision(
                action="SUPPRESS",
                priority="noise",
                reason=f"Stale: 3+ alerts for {event_type}:{entity_id} in 24h",
            )

        priority, confidence = _classify_priority(event_type, context)

        if priority == "noise":
            return AlertDecision(action="SUPPRESS", priority="noise", confidence=confidence, reason="Classified as noise")

        if priority in ("critical", "high"):
            return AlertDecision(action="SEND_NOW", priority=priority, confidence=confidence)

        # Medium/low channel posts — still send (channel is shared, batch doesn't apply)
        return AlertDecision(action="SEND_NOW", priority=priority, confidence=confidence)

    except Exception:
        logger.debug("evaluate_channel_alert error, defaulting to SEND_NOW", exc_info=True)
        return AlertDecision(action="SEND_NOW", priority="medium", reason="Fallback")


def evaluate_dm_alert(user_id: int, event_type: str, entity_id: str, message: str, context: dict | None = None, db=None) -> AlertDecision:
    """Evaluate whether a DM alert should be sent, batched, or suppressed.

    This adds intelligence gating in teams_alert_service.send_alert().
    Falls through to SEND_NOW on any error (fire-and-forget safety).
    """
    try:
        priority, confidence = _classify_priority(event_type, context)

        # Check staleness
        if _check_staleness(event_type, str(entity_id)):
            return AlertDecision(
                action="SUPPRESS",
                priority="noise",
                reason=f"Stale: repeated alerts for {entity_id}",
            )

        # Check user's priority threshold
        if db:
            threshold = _get_user_threshold(user_id, db)
            if PRIORITY_RANK.get(priority, 2) < PRIORITY_RANK.get(threshold, 2):
                return AlertDecision(
                    action="BATCH",
                    priority=priority,
                    confidence=confidence,
                    reason=f"Below user threshold ({threshold})",
                    batch_key=f"digest:{user_id}",
                )

            # Check quiet hours — only critical gets through
            if _check_quiet_hours(user_id, db):
                if priority != "critical":
                    return AlertDecision(
                        action="BATCH",
                        priority=priority,
                        confidence=confidence,
                        reason="Quiet hours — batched for digest",
                        batch_key=f"digest:{user_id}",
                    )

            # Check engagement-based downgrade
            dismissal_rate = _get_engagement_dismissal_rate(user_id, event_type, db)
            if dismissal_rate > 0.8:
                downgraded = PRIORITY_LEVELS[max(0, PRIORITY_RANK.get(priority, 2) - 1)]
                if PRIORITY_RANK.get(downgraded, 0) < PRIORITY_RANK.get(threshold, 2):
                    return AlertDecision(
                        action="BATCH",
                        priority=downgraded,
                        confidence=confidence * 0.9,
                        reason=f"Downgraded: {dismissal_rate:.0%} dismissal rate for {event_type}",
                        batch_key=f"digest:{user_id}",
                    )
                priority = downgraded

        # Decision based on final priority
        if priority in ("critical", "high"):
            return AlertDecision(action="SEND_NOW", priority=priority, confidence=confidence)
        elif priority in ("medium",):
            return AlertDecision(action="SEND_NOW", priority=priority, confidence=confidence)
        else:
            return AlertDecision(
                action="BATCH",
                priority=priority,
                confidence=confidence,
                reason="Low priority — batched for digest",
                batch_key=f"digest:{user_id}",
            )

    except Exception:
        logger.debug("evaluate_dm_alert error, defaulting to SEND_NOW", exc_info=True)
        return AlertDecision(action="SEND_NOW", priority="medium", reason="Fallback")


def record_engagement(user_id: int, event_type: str, entity_id: str, action: str,
                      delivery_method: str = "dm", ai_priority: str = "", ai_confidence: float = 0.0,
                      suppression_reason: str = "", db=None) -> None:
    """Record a notification engagement event."""
    try:
        if db is None:
            from app.database import SessionLocal
            db = SessionLocal()
            should_close = True
        else:
            should_close = False

        try:
            from app.models.notification_engagement import NotificationEngagement
            entry = NotificationEngagement(
                user_id=user_id,
                event_type=event_type,
                entity_id=str(entity_id),
                delivery_method=delivery_method,
                action=action,
                ai_priority=ai_priority or None,
                ai_confidence=ai_confidence or None,
                suppression_reason=suppression_reason or None,
            )
            db.add(entry)
            db.flush()
            if should_close:
                db.commit()
        finally:
            if should_close:
                db.close()
    except Exception:
        logger.debug("Failed to record engagement", exc_info=True)


def queue_batch_alert(user_id: int, event_type: str, entity_id: str, message: str, priority: str = "low") -> None:
    """Queue an alert for batch digest delivery via Redis."""
    import json

    r = _get_redis()
    if not r:
        return

    try:
        item = json.dumps({
            "user_id": user_id,
            "event_type": event_type,
            "entity_id": str(entity_id),
            "message": message,
            "priority": priority,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        })
        r.rpush(f"notif_batch:{user_id}", item)
        r.expire(f"notif_batch:{user_id}", 14400)  # 4h TTL
    except Exception:
        logger.debug("Failed to queue batch alert", exc_info=True)


def get_batch_queue(user_id: int) -> list[dict]:
    """Get and clear all queued batch alerts for a user."""
    import json

    r = _get_redis()
    if not r:
        return []

    key = f"notif_batch:{user_id}"
    try:
        items = []
        while True:
            raw = r.lpop(key)
            if not raw:
                break
            try:
                items.append(json.loads(raw))
            except Exception:
                continue
        return items
    except Exception:
        return []


def is_intelligence_enabled() -> bool:
    """Check if notification intelligence feature flag is on."""
    if os.environ.get("TESTING"):
        return os.environ.get("NOTIFICATION_INTELLIGENCE_ENABLED", "").lower() == "true"
    try:
        from app.services.credential_service import get_credential_cached
        val = get_credential_cached("system", "NOTIFICATION_INTELLIGENCE_ENABLED")
        return str(val).lower() == "true" if val else False
    except Exception:
        return os.environ.get("NOTIFICATION_INTELLIGENCE_ENABLED", "").lower() == "true"
