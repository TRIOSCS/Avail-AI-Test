"""Calendar Intelligence Service — scan calendar events for vendor meetings.

Detects vendor-domain attendees and trade show events from the user's
Graph API calendar. Logs findings as ActivityLog entries.

Called by: scheduler.py (_job_calendar_scan)
Depends on: utils/graph_client.py, models/intelligence.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

# Trade shows commonly attended in electronic components industry
TRADE_SHOW_KEYWORDS = [
    "electronica",
    "apec",
    "eds summit",
    "semicon",
    "ipc apex",
    "distributech",
    "arrow show",
    "avnet show",
    "embedded world",
    "ces ",
    "productronica",
]


async def scan_calendar_events(token: str, user_id: int, db: Session, lookback_days: int = 30) -> dict:
    """Scan user's calendar for vendor meetings and trade shows.

    Args:
        token: Valid Graph API access token.
        user_id: User ID for activity logging.
        db: Database session.
        lookback_days: How far back to scan.

    Returns:
        {
            events_scanned: int,
            vendor_meetings: int,
            trade_shows: int,
            activities_logged: int,
        }
    """
    from app.config import settings
    from app.utils.graph_client import GraphClient

    gc = GraphClient(token)

    start_time = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    end_time = datetime.now(timezone.utc).isoformat()

    try:
        events = await gc.get_all_pages(
            "/me/calendar/events",
            params={
                "$filter": f"start/dateTime ge '{start_time}' and start/dateTime le '{end_time}'",
                "$select": "subject,attendees,start,end,location,organizer",
                "$top": "50",
                "$orderby": "start/dateTime desc",
            },
            max_items=500,
        )
    except Exception as e:
        logger.warning("Calendar scan failed for user %d: %s", user_id, e)
        return {
            "events_scanned": 0,
            "vendor_meetings": 0,
            "trade_shows": 0,
            "activities_logged": 0,
        }

    own_domains = settings.own_domains
    vendor_meetings = 0
    trade_shows = 0
    activities_logged = 0

    for event in events:
        subject = (event.get("subject") or "").strip()
        attendees = event.get("attendees", [])

        # Check for trade show keywords
        is_trade_show = any(kw in subject.lower() for kw in TRADE_SHOW_KEYWORDS)

        # Find external (vendor) attendees
        vendor_attendees = []
        for att in attendees:
            email_data = att.get("emailAddress", {})
            email = (email_data.get("address") or "").lower()
            if not email or "@" not in email:
                continue
            domain = email.split("@")[-1]
            if domain not in own_domains:
                vendor_attendees.append(
                    {
                        "email": email,
                        "name": email_data.get("name", ""),
                        "domain": domain,
                    }
                )

        if is_trade_show:
            trade_shows += 1
            if _log_calendar_activity(db, user_id, "trade_show", subject, event, vendor_attendees):
                activities_logged += 1
        elif vendor_attendees:
            vendor_meetings += 1
            if _log_calendar_activity(db, user_id, "vendor_meeting", subject, event, vendor_attendees):
                activities_logged += 1

    if activities_logged:
        try:
            db.commit()
        except Exception as e:
            logger.warning("Calendar activities commit failed: %s", e)
            db.rollback()

    return {
        "events_scanned": len(events),
        "vendor_meetings": vendor_meetings,
        "trade_shows": trade_shows,
        "activities_logged": activities_logged,
    }


def _log_calendar_activity(
    db: Session,
    user_id: int,
    activity_type: str,
    subject: str,
    event: dict,
    vendor_attendees: list[dict],
) -> bool:
    """Create an ActivityLog entry for a calendar event.

    Returns True if a new activity was logged, False if already exists (dedup).
    """
    from app.models import ActivityLog

    start_data = event.get("start", {})
    start_str = start_data.get("dateTime", "")

    # Check if this event was already logged (by external_id)
    event_key = f"cal:{subject[:100]}:{start_str[:10]}"
    existing = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.external_id == event_key,
        )
        .first()
    )
    if existing:
        return False

    contact_emails = [a["email"] for a in vendor_attendees[:5]]
    contact_names = [a["name"] for a in vendor_attendees[:5] if a["name"]]

    import json

    notes_data = json.dumps(
        {
            "attendees": contact_emails,
            "location": (event.get("location", {}) or {}).get("displayName"),
            "start": start_str,
        }
    )

    db.add(
        ActivityLog(
            user_id=user_id,
            activity_type=activity_type,
            channel="calendar",
            subject=subject[:500],
            contact_email=contact_emails[0] if contact_emails else None,
            contact_name=contact_names[0] if contact_names else None,
            external_id=event_key,
            notes=notes_data,
            created_at=datetime.now(timezone.utc),
        )
    )
    return True
