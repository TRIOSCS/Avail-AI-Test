"""Teams notification service — surgical channel posting via Graph API.

Posts Adaptive Cards to a configured Teams channel for critical AVAIL events:
1. HOT REQUIREMENT — target value > configurable threshold ($10k default)
2. COMPETITIVE QUOTE — vendor quotes >20% below current best price
3. OWNERSHIP EXPIRING — customer ownership expires in 7 days (day-23/83)
4. STOCK LIST GOLD — auto-imported stock list matches open requirements

Business Rules:
- Rate limited: max 1 post per event type per entity per hour
- Fire-and-forget: errors logged, never raised
- Graceful degradation: if Teams not configured or API fails, returns False

Called by: scheduler.py, ownership_service.py, requisitions.py, crm.py
Depends on: utils/graph_client.py, config.py
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("avail.teams")

# Rate limit: { "event_type:entity_id" → last_posted_at }
_rate_limits: dict[str, datetime] = {}
_RATE_LIMIT_SECONDS = 3600  # 1 hour


def _is_rate_limited(event_type: str, entity_id: int | str) -> bool:
    """Check if this event+entity combo was posted within the last hour."""
    key = f"{event_type}:{entity_id}"
    last = _rate_limits.get(key)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < _RATE_LIMIT_SECONDS:
        return True
    return False


def _mark_posted(event_type: str, entity_id: int | str):
    """Record that we posted this event for rate limiting."""
    key = f"{event_type}:{entity_id}"
    _rate_limits[key] = datetime.now(timezone.utc)


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
    except Exception:
        pass  # DB not available — use env vars

    return channel_id, team_id, enabled


async def post_to_channel(team_id: str, channel_id: str, card: dict, token: str) -> bool:
    """Post an Adaptive Card to a Teams channel via Graph API.

    Uses the chatMessage endpoint: POST /teams/{id}/channels/{id}/messages
    The card is wrapped in an attachment with contentType "application/vnd.microsoft.card.adaptive".

    Returns True on success, False on any failure.
    """
    from app.utils.graph_client import GraphClient

    payload = {
        "body": {"contentType": "html", "content": "<attachment id=\"card\"></attachment>"},
        "attachments": [
            {
                "id": "card",
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }

    try:
        gc = GraphClient(token)
        result = await gc.post_json(
            f"/teams/{team_id}/channels/{channel_id}/messages", payload
        )
        if "error" in result:
            log.warning(f"Teams post failed: {result.get('detail', result.get('error'))}")
            return False
        return True
    except Exception as e:
        log.warning(f"Teams post error: {e}")
        return False


def _build_deep_link(path: str) -> str:
    """Build a deep link back to AVAIL."""
    from app.config import settings
    base = settings.app_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _make_card(title: str, subtitle: str, facts: list[dict], action_url: str, action_title: str = "View in AVAIL", accent_color: str = "attention") -> dict:
    """Build a standard Adaptive Card for Teams notifications.

    Args:
        title: Header text (e.g., "HOT REQUIREMENT")
        subtitle: Description line
        facts: List of {"title": "...", "value": "..."} pairs
        action_url: Deep link URL
        action_title: Button text
        accent_color: "attention" (red), "good" (green), "warning" (yellow), "accent" (blue)
    """
    return {
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
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": action_title,
                "url": action_url,
            }
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
#  EVENT SENDERS
# ═══════════════════════════════════════════════════════════════════════


async def send_hot_requirement_alert(
    requirement_id: int,
    mpn: str,
    target_qty: int,
    target_price: float,
    customer_name: str,
    requisition_id: int,
    token: str | None = None,
) -> bool:
    """Post alert for a high-value new requirement.

    Triggered when target_qty * target_price > threshold (default $10,000).
    """
    channel_id, team_id, enabled = _get_teams_config()
    if not enabled:
        return False
    if _is_rate_limited("hot_requirement", requirement_id):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    total_value = target_qty * target_price
    card = _make_card(
        title="HOT REQUIREMENT",
        subtitle=f"High-value requirement: {mpn} (${total_value:,.0f})",
        facts=[
            {"title": "MPN", "value": mpn},
            {"title": "Quantity", "value": f"{target_qty:,}"},
            {"title": "Target Price", "value": f"${target_price:,.4f}"},
            {"title": "Total Value", "value": f"${total_value:,.0f}"},
            {"title": "Customer", "value": customer_name or "—"},
        ],
        action_url=_build_deep_link(f"#requisition/{requisition_id}"),
        action_title="View Requirement",
        accent_color="attention",
    )

    ok = await post_to_channel(team_id, channel_id, card, token)
    if ok:
        _mark_posted("hot_requirement", requirement_id)
    return ok


async def send_competitive_quote_alert(
    offer_id: int,
    mpn: str,
    vendor_name: str,
    offer_price: float,
    best_price: float,
    requisition_id: int,
    token: str | None = None,
) -> bool:
    """Post alert when a vendor quotes >20% below current best price."""
    channel_id, team_id, enabled = _get_teams_config()
    if not enabled:
        return False
    if _is_rate_limited("competitive_quote", offer_id):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    savings_pct = ((best_price - offer_price) / best_price) * 100 if best_price else 0
    card = _make_card(
        title="COMPETITIVE QUOTE",
        subtitle=f"{vendor_name} undercuts best price by {savings_pct:.0f}%",
        facts=[
            {"title": "MPN", "value": mpn},
            {"title": "Vendor", "value": vendor_name},
            {"title": "Offer Price", "value": f"${offer_price:,.4f}"},
            {"title": "Previous Best", "value": f"${best_price:,.4f}"},
            {"title": "Savings", "value": f"{savings_pct:.1f}%"},
        ],
        action_url=_build_deep_link(f"#requisition/{requisition_id}"),
        action_title="View Offers",
        accent_color="good",
    )

    ok = await post_to_channel(team_id, channel_id, card, token)
    if ok:
        _mark_posted("competitive_quote", offer_id)
    return ok


async def send_ownership_warning(
    company_id: int,
    company_name: str,
    owner_name: str,
    days_remaining: int,
    token: str | None = None,
) -> bool:
    """Post alert when customer ownership is about to expire."""
    channel_id, team_id, enabled = _get_teams_config()
    if not enabled:
        return False
    if _is_rate_limited("ownership_expiring", company_id):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    card = _make_card(
        title="OWNERSHIP EXPIRING",
        subtitle=f"{company_name} ownership expires in {days_remaining} days",
        facts=[
            {"title": "Company", "value": company_name},
            {"title": "Owner", "value": owner_name},
            {"title": "Days Remaining", "value": str(days_remaining)},
            {"title": "Action Needed", "value": "Log activity to retain ownership"},
        ],
        action_url=_build_deep_link(f"#company/{company_id}"),
        action_title="View Company",
        accent_color="warning",
    )

    ok = await post_to_channel(team_id, channel_id, card, token)
    if ok:
        _mark_posted("ownership_expiring", company_id)
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
    channel_id, team_id, enabled = _get_teams_config()
    if not enabled:
        return False

    # Rate limit by filename hash to avoid repeat alerts for same file
    rate_key = f"{vendor_name}:{filename}"
    if _is_rate_limited("stock_match", rate_key):
        return False

    if not token:
        token = await _get_system_token()
        if not token:
            return False

    mpn_list = ", ".join(m["mpn"] for m in matches[:5])
    if len(matches) > 5:
        mpn_list += f" (+{len(matches) - 5} more)"

    # Link to first matching requisition
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

    ok = await post_to_channel(team_id, channel_id, card, token)
    if ok:
        _mark_posted("stock_match", rate_key)
    return ok


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


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
            admin = (
                db.query(User)
                .filter(User.access_token.isnot(None), User.m365_connected.is_(True))
                .first()
            )
            if not admin:
                return None
            return await get_valid_token(admin, db)
        finally:
            db.close()
    except Exception as e:
        log.warning(f"Failed to get system token for Teams: {e}")
        return None


def clear_rate_limits():
    """Clear rate limit cache (for testing)."""
    _rate_limits.clear()
