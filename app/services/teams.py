"""Teams notification service — channel posting via Graph API with Redis rate limiting.

Posts Adaptive Cards to configured Teams channels for critical AVAIL events:
1. COMPETITIVE QUOTE — vendor quotes >20% below current best price
2. STOCK LIST GOLD — auto-imported stock list matches open requirements
3. BUY PLAN lifecycle (submit/approve/reject/complete/cancel)
4. BUY PLAN ESCALATION — approval pending >8h
5. WEEKLY DIGEST — Monday 8am summary

Business Rules:
- Rate limited via Redis (1h TTL, fallback to in-memory if Redis unavailable)
- Per-event-type channel routing (falls back to default channel)
- Fire-and-forget: errors logged, never raised
- Graceful degradation: if Teams not configured or API fails, returns False
- All posts logged to teams_notification_log table

Called by: scheduler.py, crm.py, buyplan_notifications.py, buyplan_v3_notifications.py
Depends on: utils/graph_client.py, config.py, cache/intel_cache.py (Redis pattern)
"""

from datetime import datetime, timezone

from loguru import logger

from .teams_action_tokens import create_teams_action_token

# In-memory fallback for rate limiting when Redis is unavailable
_rate_limits: dict[str, datetime] = {}
_RATE_LIMIT_SECONDS = 3600  # 1 hour
_REDIS_PREFIX = "teams_rl:"

# Event type → channel config key mapping
EVENT_CHANNEL_MAP = {
    "competitive_quote": "teams_channel_quotes",
    "stock_match": "teams_channel_inventory",
    "buyplan_submitted": "teams_channel_buyplan",
    "buyplan_approved": "teams_channel_buyplan",
    "buyplan_rejected": "teams_channel_buyplan",
    "buyplan_completed": "teams_channel_buyplan",
    "buyplan_cancelled": "teams_channel_buyplan",
    "buyplan_escalation": "teams_channel_buyplan",
    "weekly_digest": "teams_channel_hot",
}


def _get_redis():
    """Get Redis client for rate limiting. Returns None if unavailable."""
    import os

    if os.environ.get("TESTING"):
        return None
    try:
        from app.cache.intel_cache import _get_redis as _cache_get_redis

        return _cache_get_redis()
    except Exception:
        return None


def _is_rate_limited(event_type: str, entity_id: int | str) -> bool:
    """Check if this event+entity combo was posted within the last hour.

    Uses Redis SETEX with 1h TTL (survives app restarts).
    Falls back to in-memory dict if Redis is unavailable.
    """
    key = f"{event_type}:{entity_id}"
    r = _get_redis()
    if r:
        try:
            return r.exists(f"{_REDIS_PREFIX}{key}") > 0
        except Exception:
            logger.debug("Redis rate-limit check failed, falling back to in-memory", exc_info=True)
    # Fallback to in-memory
    last = _rate_limits.get(key)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < _RATE_LIMIT_SECONDS:
        return True
    return False


def _mark_posted(event_type: str, entity_id: int | str):
    """Record that we posted this event for rate limiting.

    Sets a Redis key with 1h TTL. Falls back to in-memory dict only when Redis is unavailable.
    """
    key = f"{event_type}:{entity_id}"
    r = _get_redis()
    if r:
        try:
            r.setex(f"{_REDIS_PREFIX}{key}", _RATE_LIMIT_SECONDS, "1")
            return
        except Exception:
            logger.debug("Redis rate-limit set failed, falling back to in-memory", exc_info=True)
    # In-memory fallback — prune stale entries periodically to prevent unbounded growth
    now = datetime.now(timezone.utc)
    if len(_rate_limits) > 1000:
        stale = [k for k, v in _rate_limits.items() if (now - v).total_seconds() >= _RATE_LIMIT_SECONDS]
        for k in stale:
            del _rate_limits[k]
    _rate_limits[key] = now


def _get_teams_config() -> tuple[str, str, bool]:
    """Get Teams channel_id, team_id, and enabled status.

    Checks SystemConfig DB first, falls back to env vars.
    Returns (channel_id, team_id, enabled).
    """
    from app.config import settings

    channel_id = settings.teams_channel_id
    team_id = settings.teams_team_id
    enabled = bool(channel_id and team_id)

    # Try cached SystemConfig (runtime override, 5-min TTL)
    try:
        from app.database import SessionLocal
        from app.services.admin_service import get_config_values

        db = SessionLocal()
        try:
            cfg = get_config_values(db, ["teams_channel_id", "teams_team_id", "teams_enabled"])
            if cfg.get("teams_channel_id"):
                channel_id = cfg["teams_channel_id"]
            if cfg.get("teams_team_id"):
                team_id = cfg["teams_team_id"]
            if "teams_enabled" in cfg:
                enabled = cfg["teams_enabled"].lower() == "true"
        finally:
            db.close()
    except Exception as e:
        logger.debug("Teams config: DB lookup failed, using env vars: %s", e)

    return channel_id, team_id, enabled


def _get_channel_for_event(event_type: str) -> tuple[str, str, bool]:
    """Get the channel for a specific event type, falling back to default.

    Fetches default config AND per-event routing in a single DB session.
    Returns (channel_id, team_id, enabled).
    """
    from app.config import settings

    channel_id = settings.teams_channel_id
    team_id = settings.teams_team_id
    enabled = bool(channel_id and team_id)

    config_key = EVENT_CHANNEL_MAP.get(event_type)
    # Fetch all needed keys in one DB session
    keys_to_fetch = ["teams_channel_id", "teams_team_id", "teams_enabled"]
    if config_key:
        keys_to_fetch.extend([config_key, f"{config_key}_team"])

    try:
        from app.database import SessionLocal
        from app.services.admin_service import get_config_values

        db = SessionLocal()
        try:
            cfg = get_config_values(db, keys_to_fetch)
        finally:
            db.close()

        # Apply default config
        if cfg.get("teams_channel_id"):
            channel_id = cfg["teams_channel_id"]
        if cfg.get("teams_team_id"):
            team_id = cfg["teams_team_id"]
        if "teams_enabled" in cfg:
            enabled = cfg["teams_enabled"].lower() == "true"

        if not enabled:
            return channel_id, team_id, enabled

        # Apply per-event routing override
        if config_key:
            if cfg.get(config_key):
                channel_id = cfg[config_key]
            if cfg.get(f"{config_key}_team"):
                team_id = cfg[f"{config_key}_team"]
    except Exception as e:
        logger.debug("Teams config: DB lookup failed, using env vars: %s", e)

    return channel_id, team_id, enabled


async def post_to_channel(
    team_id: str,
    channel_id: str,
    card: dict,
    token: str,
    event_type: str = "",
    entity_id: int | str = 0,
    entity_name: str = "",
) -> bool:
    """Post an Adaptive Card to a Teams channel via Graph API.

    Uses the chatMessage endpoint: POST /teams/{id}/channels/{id}/messages
    The card is wrapped in an attachment with contentType "application/vnd.microsoft.card.adaptive".
    Logs the result to teams_notification_log table.

    Returns True on success, False on any failure.
    """
    import json as _json

    from app.utils.graph_client import GraphClient

    payload = {
        "body": {"contentType": "html", "content": '<attachment id="card"></attachment>'},
        "attachments": [
            {
                "id": "card",
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": _json.dumps(card),
            }
        ],
    }

    success = False
    error_msg = None
    try:
        gc = GraphClient(token)
        result = await gc.post_json(f"/teams/{team_id}/channels/{channel_id}/messages", payload)
        if "error" in result:
            error_msg = str(result.get("detail", result.get("error")))
            logger.warning(f"Teams post failed: {error_msg}")
        else:
            success = True
    except Exception as e:
        error_msg = str(e)[:500]
        logger.warning(f"Teams post error: {e}")

    # Log to audit table (fire-and-forget)
    if event_type:
        _log_notification(event_type, entity_id, entity_name, channel_id, success, error_msg)

    return success


def _build_deep_link(path: str) -> str:
    """Build a deep link back to AVAIL."""
    from app.config import settings

    base = settings.app_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# ═══════════════════════════════════════════════════════════════════════
#  CARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════


def _make_card_with_actions(
    title: str,
    subtitle: str,
    facts: list[dict],
    actions: list[dict],
    accent_color: str = "attention",
    mentions: list[dict] | None = None,
) -> dict:
    """Build an Adaptive Card with custom actions (buttons) and optional @mentions.

    Actions can be Action.OpenUrl or Action.Submit (for interactive cards).
    Mentions list: [{"text": "<at>John</at>", "mentioned": {"id": "john@co.com", "name": "John"}}]
    """
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Medium",
                "color": accent_color,
            },
            {
                "type": "TextBlock",
                "text": subtitle,
                "wrap": True,
                "spacing": "Small",
            },
            {
                "type": "FactSet",
                "facts": facts,
                "spacing": "Medium",
            },
        ],
        "actions": actions,
    }
    if mentions:
        card["msteams"] = {"entities": mentions}
    return card


def _make_card(
    title: str,
    subtitle: str,
    facts: list[dict],
    action_url: str,
    action_title: str = "View in AVAIL",
    accent_color: str = "attention",
) -> dict:
    """Build a standard Adaptive Card with a single OpenUrl action.

    Delegates to _make_card_with_actions to avoid duplicating card structure.
    """
    return _make_card_with_actions(
        title=title,
        subtitle=subtitle,
        facts=facts,
        actions=[{"type": "Action.OpenUrl", "title": action_title, "url": action_url}],
        accent_color=accent_color,
    )


def _build_mention(email: str, name: str) -> tuple[str, dict]:
    """Build a Teams @mention text and entity for an Adaptive Card.

    Returns (mention_text, mention_entity) to insert into card subtitle and msteams.entities.
    """
    mention_text = f"<at>{name}</at>"
    mention_entity = {
        "type": "mention",
        "text": mention_text,
        "mentioned": {"id": email, "name": name},
    }
    return mention_text, mention_entity


# ═══════════════════════════════════════════════════════════════════════
#  ORIGINAL 4 EVENT SENDERS (upgraded with channel routing + audit log)
# ═══════════════════════════════════════════════════════════════════════


def _intelligence_gate(event_type: str, entity_id, context: dict | None = None) -> bool:
    """Check rate limiting — returns False if alert should be suppressed."""
    return not _is_rate_limited(event_type, entity_id)


async def send_competitive_quote_alert(
    offer_id: int,
    mpn: str,
    vendor_name: str,
    offer_price: float,
    best_price: float,
    requisition_id: int,
    token: str | None = None,
    top_vendors: list[dict] | None = None,
    creator_email: str = "",
    creator_name: str = "",
) -> bool:
    """Post alert when a vendor quotes >20% below current best price.

    If top_vendors is provided, shows top 3 side-by-side comparison.
    Optionally @mentions the requisition creator.
    """
    channel_id, team_id, enabled = _get_channel_for_event("competitive_quote")
    if not enabled:
        return False
    savings_pct = ((best_price - offer_price) / best_price) * 100 if best_price else 0
    if not _intelligence_gate("competitive_quote", offer_id, {"savings_pct": savings_pct}):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    mentions = []
    subtitle = f"{vendor_name} undercuts best price by {savings_pct:.0f}%"
    if creator_email and creator_name:
        mention_text, mention_entity = _build_mention(creator_email, creator_name)
        subtitle = f"{mention_text} — {subtitle}"
        mentions.append(mention_entity)

    # Richer facts: show top 3 vendors side-by-side if available
    facts = [
        {"title": "MPN", "value": mpn},
        {"title": "Vendor", "value": vendor_name},
        {"title": "Offer Price", "value": f"${offer_price:,.4f}"},
        {"title": "Previous Best", "value": f"${best_price:,.4f}"},
        {"title": "Savings", "value": f"{savings_pct:.1f}%"},
    ]
    if top_vendors:
        for i, tv in enumerate(top_vendors[:3], 1):
            facts.append({"title": f"#{i} {tv.get('vendor', '')}", "value": f"${tv.get('price', 0):,.4f}"})

    card = _make_card(
        title="COMPETITIVE QUOTE",
        subtitle=subtitle,
        facts=facts,
        action_url=_build_deep_link(f"#requisition/{requisition_id}"),
        action_title="View Offers",
        accent_color="good",
    )
    if mentions:
        card["msteams"] = {"entities": mentions}

    ok = await post_to_channel(
        team_id,
        channel_id,
        card,
        token,
        event_type="competitive_quote",
        entity_id=offer_id,
        entity_name=mpn,
    )
    if ok:
        _mark_posted("competitive_quote", offer_id)
    return ok


async def send_stock_match_alert(
    matches: list[dict],
    filename: str,
    vendor_name: str,
    token: str | None = None,
) -> bool:
    """Post alert when auto-imported stock list contains parts matching open requirements.

    matches: list of {"mpn": str, "requirement_id": int, "requisition_id": int}
    """
    channel_id, team_id, enabled = _get_channel_for_event("stock_match")
    if not enabled:
        return False

    rate_key = f"{vendor_name}:{filename}"
    if not _intelligence_gate("stock_match", rate_key):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    mpn_list = ", ".join(m["mpn"] for m in matches[:5])
    if len(matches) > 5:
        mpn_list += f" (+{len(matches) - 5} more)"

    first_req = matches[0].get("requisition_id", 0) if matches else 0

    card = _make_card(
        title="STOCK LIST GOLD",
        subtitle=f"{vendor_name}'s stock list matches {len(matches)} open requirement(s)",
        facts=[
            {"title": "Vendor", "value": vendor_name},
            {"title": "File", "value": filename},
            {"title": "Matches", "value": str(len(matches))},
            {"title": "MPNs", "value": mpn_list},
        ],
        action_url=_build_deep_link(f"#requisition/{first_req}") if first_req else _build_deep_link("#"),
        action_title="View Requirements",
        accent_color="accent",
    )

    ok = await post_to_channel(
        team_id,
        channel_id,
        card,
        token,
        event_type="stock_match",
        entity_id=rate_key,
        entity_name=filename,
    )
    if ok:
        _mark_posted("stock_match", rate_key)
    return ok


# ═══════════════════════════════════════════════════════════════════════
#  BUY PLAN EVENT SENDERS (unified Graph API Adaptive Cards)
# ═══════════════════════════════════════════════════════════════════════


async def send_buyplan_card(
    plan_id: int,
    event: str,
    subtitle: str,
    facts: list[dict],
    accent_color: str = "accent",
    action_buttons: list[dict] | None = None,
    mention_emails: list[tuple[str, str]] | None = None,
    token: str | None = None,
) -> bool:
    """Unified buy plan card sender.

    event: buyplan_submitted, buyplan_approved, buyplan_rejected, buyplan_completed, buyplan_cancelled
    action_buttons: optional list of Action.Submit dicts for interactive cards
    mention_emails: list of (email, name) tuples for @mentions
    """
    event_type = event if event.startswith("buyplan_") else f"buyplan_{event}"
    channel_id, team_id, enabled = _get_channel_for_event(event_type)
    if not enabled:
        return False
    if not _intelligence_gate(event_type, plan_id):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    title_map = {
        "buyplan_submitted": "BUY PLAN — APPROVAL REQUIRED",
        "buyplan_approved": "BUY PLAN — APPROVED",
        "buyplan_rejected": "BUY PLAN — REJECTED",
        "buyplan_completed": "BUY PLAN — COMPLETE",
        "buyplan_cancelled": "BUY PLAN — CANCELLED",
        "buyplan_escalation": "BUY PLAN — ESCALATION",
    }
    color_map = {
        "buyplan_submitted": "warning",
        "buyplan_approved": "good",
        "buyplan_rejected": "attention",
        "buyplan_completed": "good",
        "buyplan_cancelled": "attention",
        "buyplan_escalation": "attention",
    }

    mentions = []
    if mention_emails:
        for email, name in mention_emails:
            mt, me = _build_mention(email, name)
            subtitle = f"{mt} {subtitle}"
            mentions.append(me)

    actions = action_buttons or [
        {"type": "Action.OpenUrl", "title": "View in AVAIL", "url": _build_deep_link(f"#buyplan/{plan_id}")},
    ]

    card = _make_card_with_actions(
        title=title_map.get(event_type, event_type.upper()),
        subtitle=subtitle,
        facts=facts,
        actions=actions,
        accent_color=color_map.get(event_type, accent_color),
        mentions=mentions or None,
    )

    ok = await post_to_channel(
        team_id,
        channel_id,
        card,
        token,
        event_type=event_type,
        entity_id=plan_id,
        entity_name=f"Buy Plan #{plan_id}",
    )
    if ok:
        _mark_posted(event_type, plan_id)
    return ok


async def send_buyplan_approval_card(
    plan_id: int,
    submitter_name: str,
    total_cost: float,
    line_count: int,
    requisition_id: int,
    admin_emails: list[tuple[str, str]] | None = None,
    token: str | None = None,
) -> bool:
    """Interactive buy plan approval card with Approve/Reject buttons."""
    approve_token = create_teams_action_token(plan_id, "buyplan_approve")
    reject_token = create_teams_action_token(plan_id, "buyplan_reject")

    actions = [
        {
            "type": "Action.Submit",
            "title": "Approve",
            "style": "positive",
            "data": {"action": "buyplan_approve", "plan_id": plan_id, "action_token": approve_token},
        },
        {
            "type": "Action.Submit",
            "title": "Reject",
            "style": "destructive",
            "data": {"action": "buyplan_reject", "plan_id": plan_id, "action_token": reject_token},
        },
        {
            "type": "Action.OpenUrl",
            "title": "View Details",
            "url": _build_deep_link(f"#buyplan/{plan_id}"),
        },
    ]

    return await send_buyplan_card(
        plan_id=plan_id,
        event="buyplan_submitted",
        subtitle=f"Submitted by {submitter_name} — ${total_cost:,.2f}",
        facts=[
            {"title": "Submitter", "value": submitter_name},
            {"title": "Total Cost", "value": f"${total_cost:,.2f}"},
            {"title": "Line Items", "value": str(line_count)},
            {"title": "Requisition", "value": f"#{requisition_id}"},
        ],
        action_buttons=actions,
        mention_emails=admin_emails,
        token=token,
    )


async def send_buyplan_escalation_alert(
    plan_id: int,
    submitter_name: str,
    hours_pending: float,
    total_cost: float,
    admin_emails: list[tuple[str, str]] | None = None,
    token: str | None = None,
) -> bool:
    """Post alert when a buy plan approval has been pending >8h."""
    return await send_buyplan_card(
        plan_id=plan_id,
        event="buyplan_escalation",
        subtitle=f"Approval pending {hours_pending:.0f}h — submitted by {submitter_name}",
        facts=[
            {"title": "Submitter", "value": submitter_name},
            {"title": "Total Cost", "value": f"${total_cost:,.2f}"},
            {"title": "Hours Pending", "value": f"{hours_pending:.1f}h"},
            {"title": "Action Needed", "value": "Approve or reject to unblock deal"},
        ],
        mention_emails=admin_emails,
        token=token,
    )


async def send_weekly_digest(
    stats: dict,
    token: str | None = None,
) -> bool:
    """Post Monday 8am summary card with key KPIs.

    stats: {reqs_created, reqs_won, reqs_lost, total_value_won, quotes_sent,
            offers_received, tickets_opened, tickets_resolved, connectors_up, connectors_total}
    """
    channel_id, team_id, enabled = _get_channel_for_event("weekly_digest")
    if not enabled:
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    card = _make_card(
        title="WEEKLY DIGEST",
        subtitle="Last 7 days at a glance",
        facts=[
            {"title": "Reqs Created", "value": str(stats.get("reqs_created", 0))},
            {"title": "Deals Won", "value": str(stats.get("reqs_won", 0))},
            {"title": "Deals Lost", "value": str(stats.get("reqs_lost", 0))},
            {"title": "Value Won", "value": f"${stats.get('total_value_won', 0):,.0f}"},
            {"title": "Quotes Sent", "value": str(stats.get("quotes_sent", 0))},
            {"title": "Offers Received", "value": str(stats.get("offers_received", 0))},
            {"title": "Tickets Opened", "value": str(stats.get("tickets_opened", 0))},
            {"title": "Tickets Resolved", "value": str(stats.get("tickets_resolved", 0))},
            {"title": "Connectors Up", "value": f"{stats.get('connectors_up', 0)}/{stats.get('connectors_total', 0)}"},
        ],
        action_url=_build_deep_link("#"),
        action_title="Open AVAIL",
        accent_color="accent",
    )

    ok = await post_to_channel(
        team_id,
        channel_id,
        card,
        token,
        event_type="weekly_digest",
        entity_id="weekly",
        entity_name="Weekly Digest",
    )
    return ok


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _log_notification(
    event_type: str,
    entity_id: int | str,
    entity_name: str,
    channel_id: str,
    success: bool,
    error_msg: str | None,
):
    """Log a notification to the teams_notification_log table (fire-and-forget)."""
    try:
        from app.database import SessionLocal
        from app.models.teams_notification_log import TeamsNotificationLog

        db = SessionLocal()
        try:
            db.add(
                TeamsNotificationLog(
                    event_type=event_type,
                    entity_id=str(entity_id),
                    entity_name=str(entity_name)[:200],
                    channel_id=channel_id,
                    success=success,
                    error_msg=error_msg[:500] if error_msg else None,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug("Failed to log Teams notification: %s", e)


async def _get_system_token() -> str | None:
    """Get a valid Graph API token from any connected admin user.

    Used for application-level Teams posting when no user token is available.
    """
    try:
        from app.database import SessionLocal
        from app.models import User
        from app.scheduler import get_valid_token

        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.access_token.isnot(None), User.m365_connected.is_(True)).first()
            if not admin:
                return None
            return await get_valid_token(admin, db)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to get system token for Teams: {e}")
        return None


def clear_rate_limits():
    """Clear rate limit cache (for testing)."""
    _rate_limits.clear()


def get_notification_log(limit: int = 50) -> list[dict]:
    """Get recent Teams notification log entries for admin dashboard."""
    try:
        from app.database import SessionLocal
        from app.models.teams_notification_log import TeamsNotificationLog

        db = SessionLocal()
        try:
            rows = db.query(TeamsNotificationLog).order_by(TeamsNotificationLog.created_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "entity_id": r.entity_id,
                    "entity_name": r.entity_name,
                    "channel_id": r.channel_id,
                    "success": r.success,
                    "error_msg": r.error_msg,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to read notification log: %s", e)
        return []
