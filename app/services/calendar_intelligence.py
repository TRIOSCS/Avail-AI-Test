"""Calendar Intelligence Service — scan calendar events for meetings.

Detects external attendees from the user's Graph API calendar and creates
first-class ActivityLog rows (ActivityType.MEETING, Channel.CALENDAR) linked
to the matched SiteContact / Company or VendorCard.  Deduplicates on the
Graph event id so re-scans are idempotent.

Called by: jobs/email_jobs.py (_job_calendar_scan)
Depends on: utils/graph_client.py, services/activity_service.py
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


def _parse_graph_dt(dt_str: str | None) -> datetime | None:
    """Parse a Graph API dateTime string to a UTC datetime, or None on failure."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


async def scan_calendar_events(token: str, user_id: int, db: Session, lookback_days: int = 30) -> dict:
    """Scan user's calendar for meetings with external contacts.

    Creates first-class ActivityLog rows via log_meeting_activity for each
    event that has at least one matched external attendee.

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

    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(days=lookback_days)).isoformat()
    end_time = now.isoformat()

    try:
        events = await gc.get_all_pages(
            "/me/calendar/events",
            params={
                "$filter": f"start/dateTime ge '{start_time}' and start/dateTime le '{end_time}'",
                "$select": "id,subject,attendees,start,end,location,organizer",
                "$top": "50",
                "$orderby": "start/dateTime desc",
            },
            max_items=500,
        )
    except Exception as e:
        logger.warning("Calendar scan failed for user {}: {}", user_id, e)
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
        graph_event_id = event.get("id") or ""

        # Collect all attendee emails for log_meeting_activity to filter/match.
        attendee_emails = []
        for att in attendees:
            email_data = att.get("emailAddress", {})
            email = (email_data.get("address") or "").strip().lower()
            if email and "@" in email:
                attendee_emails.append(email)

        # Organizer email
        organizer_data = event.get("organizer", {}).get("emailAddress", {})
        organizer_email = (organizer_data.get("address") or "").strip().lower() or None

        # Start / end times
        start_dt = _parse_graph_dt(event.get("start", {}).get("dateTime"))
        end_dt = _parse_graph_dt(event.get("end", {}).get("dateTime"))
        if start_dt is None:
            continue  # Can't stamp occurred_at without a start time
        if end_dt is None:
            end_dt = start_dt

        location_name = (event.get("location", {}) or {}).get("displayName") or None

        # Classify as trade show or regular meeting
        is_trade_show = any(kw in subject.lower() for kw in TRADE_SHOW_KEYWORDS)

        # Count external (non-own-domain) attendees to decide whether to log
        has_external = any(email.split("@")[-1] not in own_domains for email in attendee_emails if "@" in email)

        if is_trade_show:
            trade_shows += 1
        elif has_external:
            vendor_meetings += 1

        if (is_trade_show or has_external) and graph_event_id:
            rows = _log_calendar_activity(
                db=db,
                user_id=user_id,
                graph_event_id=graph_event_id,
                subject=subject,
                start_dt=start_dt,
                end_dt=end_dt,
                organizer_email=organizer_email,
                attendee_emails=attendee_emails,
                location=location_name,
            )
            activities_logged += len(rows)

    if activities_logged:
        try:
            db.commit()
        except Exception as e:
            logger.warning("Calendar activities commit failed: {}", e)
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
    graph_event_id: str,
    subject: str,
    start_dt: datetime,
    end_dt: datetime,
    organizer_email: str | None,
    attendee_emails: list[str],
    location: str | None,
) -> list:
    """Create ActivityLog entries for a calendar event via log_meeting_activity.

    Returns the list of rows created (one per matched external entity). Returns [] when
    the event was already logged (idempotent dedup on graph_event_id) or when no
    attendee matched a known contact / company.
    """
    from app.services.activity_service import log_meeting_activity

    return log_meeting_activity(
        user_id=user_id,
        graph_event_id=graph_event_id,
        subject=subject,
        start_dt=start_dt,
        end_dt=end_dt,
        organizer_email=organizer_email,
        attendee_emails=attendee_emails,
        location=location,
        db=db,
    )
