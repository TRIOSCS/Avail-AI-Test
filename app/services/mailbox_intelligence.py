"""Mailbox Intelligence Service — fetch and apply user mailbox settings from Graph.

Pulls timezone, working hours, and auto-reply status from
GET /me/mailboxSettings to schedule RFQs during vendor business hours.

Called by: routers/auth.py (on login/callback), scheduler.py
Depends on: utils/graph_client.py, models/auth.py
"""

from loguru import logger
from sqlalchemy.orm import Session


async def fetch_and_store_mailbox_settings(token: str, user, db: Session) -> dict | None:
    """Fetch mailbox settings from Graph API and persist to User model.

    Args:
        token: Valid Graph API access token.
        user: User ORM instance to update.
        db: Database session.

    Returns:
        Dict with extracted settings, or None on failure.
    """
    from app.utils.graph_client import GraphClient

    gc = GraphClient(token)
    try:
        data = await gc.get_json(
            "/me/mailboxSettings",
            params={"$select": "timeZone,workingHours,automaticRepliesSetting"},
        )
    except Exception as e:
        logger.warning("Failed to fetch mailbox settings for %s: %s", user.email, e)
        return None

    if not data or "error" in data:
        logger.debug("Mailbox settings empty or error for %s", user.email)
        return None

    # Extract timezone
    tz = data.get("timeZone")
    if tz:
        user.timezone = tz

    # Extract working hours
    working_hours = data.get("workingHours")
    if working_hours:
        start_time = working_hours.get("startTime")
        end_time = working_hours.get("endTime")
        if start_time:
            user.working_hours_start = start_time[:5]  # "08:00:00.0000000" → "08:00"
        if end_time:
            user.working_hours_end = end_time[:5]

    db.flush()

    result = {
        "timezone": user.timezone,
        "working_hours_start": user.working_hours_start,
        "working_hours_end": user.working_hours_end,
    }

    # Check auto-reply status (OOO)
    auto_reply = data.get("automaticRepliesSetting", {})
    auto_reply_status = auto_reply.get("status", "disabled")
    result["auto_reply_status"] = auto_reply_status

    logger.info(
        "Mailbox settings for %s: tz=%s, hours=%s-%s, auto_reply=%s",
        user.email,
        user.timezone,
        user.working_hours_start,
        user.working_hours_end,
        auto_reply_status,
    )

    return result


def is_within_working_hours(user, target_hour: int) -> bool:
    """Check if a given hour falls within the user's working hours.

    Args:
        user: User with working_hours_start/end set.
        target_hour: Hour in 24h format (0-23).

    Returns:
        True if within working hours, or True if no hours configured.
    """
    if not user.working_hours_start or not user.working_hours_end:
        return True

    try:
        start = int(user.working_hours_start.split(":")[0])
        end = int(user.working_hours_end.split(":")[0])
        return start <= target_hour < end
    except (ValueError, IndexError):
        return True
