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
from sqlalchemy import select
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


async def scan_calendar_events(token: str, user_id: int | None, db: Session, lookback_days: int = 30) -> dict:
    """Scan user's calendar for meetings with external contacts (Delta Query).

    Uses Microsoft Graph ``/me/calendarView/delta`` so each run only fetches
    events CHANGED since the previous scan instead of re-pulling the whole
    window.  The delta token is persisted per user in ``SyncState`` (folder
    ``calendar_scan``), reusing the same plumbing as contacts / sent-folder sync.

    - Initial sync (no stored token): pages ``@odata.nextLink`` over the
      ``[now - lookback_days, now]`` window until ``@odata.deltaLink``; the
      deltaLink is stored for next time.
    - Incremental sync (stored token): calls the stored deltaLink; changed
      events are upserted via ``log_meeting_activity`` (unchanged dedupe/fields),
      and ``@removed`` entries delete the matching local ActivityLog rows.
    - Token expiry: Graph returns 410 Gone (``GraphSyncStateExpired``) when the
      delta token is too old — the stored token is discarded and a fresh full
      initial delta sync runs, without crashing.

    Args:
        token: Valid Graph API access token.
        user_id: User ID for activity logging + SyncState scoping. When None
            (callers without a persisted identity), SyncState is skipped and
            every run behaves like a stateless initial delta pull.
        db: Database session.
        lookback_days: How far back the delta window starts.

    Returns:
        {
            events_scanned: int,   # changed event payloads processed
            vendor_meetings: int,
            trade_shows: int,
            activities_logged: int,
            events_removed: int,   # local rows deleted for @removed entries
        }
    """
    from app.config import settings
    from app.models import SyncState
    from app.utils.graph_client import GraphClient, GraphSyncStateExpired

    gc = GraphClient(token)

    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(days=lookback_days)).isoformat()
    end_time = now.isoformat()

    # calendarView/delta takes the window as startDateTime/endDateTime query
    # params (NOT $filter); $orderby/$top are unsupported on delta.
    delta_params = {
        "startDateTime": start_time,
        "endDateTime": end_time,
        "$select": "id,subject,attendees,start,end,location,organizer",
    }

    def _zero() -> dict:
        return {
            "events_scanned": 0,
            "vendor_meetings": 0,
            "trade_shows": 0,
            "activities_logged": 0,
            "events_removed": 0,
        }

    # Load the persisted delta token for incremental sync (per user per folder).
    folder_key = "calendar_scan"
    sync_state = None
    delta_token: str | None = None
    if user_id is not None:
        sync_state = db.scalars(
            select(SyncState).where(SyncState.user_id == user_id, SyncState.folder == folder_key)
        ).first()
        delta_token = sync_state.delta_token if sync_state else None

    token_reset = False
    try:
        events, new_token = await gc.delta_query(
            "/me/calendarView/delta",
            delta_token=delta_token,
            params=delta_params,
            max_items=500,
        )
    except GraphSyncStateExpired:
        # 410 Gone — the stored delta token is too old. Discard it and fall back
        # to a full initial delta sync (do NOT crash).
        token_reset = True
        logger.warning("Calendar delta token expired for user {} — discarding token, full resync", user_id)
        if sync_state:
            sync_state.delta_token = None
            db.flush()
        try:
            events, new_token = await gc.delta_query(
                "/me/calendarView/delta",
                delta_token=None,
                params=delta_params,
                max_items=500,
            )
        except Exception as e:
            logger.warning("Calendar full resync failed for user {}: {}", user_id, e)
            return _zero()
    except Exception as e:
        logger.warning("Calendar scan failed for user {}: {}", user_id, e)
        return _zero()

    own_domains = settings.own_domains
    vendor_meetings = 0
    trade_shows = 0
    activities_logged = 0
    events_removed = 0
    events_changed = 0

    for event in events:
        # @removed entry — the event was deleted/cancelled upstream. Remove the
        # matching local ActivityLog row(s) so the timeline stays in sync.
        if "@removed" in event:
            removed_id = (event.get("id") or "").strip()
            if removed_id:
                events_removed += _remove_calendar_activity(db, removed_id)
            continue

        events_changed += 1
        subject = (event.get("subject") or "").strip()
        attendees = event.get("attendees", [])
        # Prefer the Graph-native stable id; synthesise a fallback key from
        # subject + date so older/mocked events (no "id" field) still dedup.
        _raw_id = (event.get("id") or "").strip()
        graph_event_id = (
            _raw_id if _raw_id else f"fallback:{subject[:80]}:{event.get('start', {}).get('dateTime', '')[:10]}"
        )

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

        if is_trade_show or has_external:
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

    # Persist the new delta link so the next run only fetches changes.
    if new_token and user_id is not None:
        if sync_state:
            sync_state.delta_token = new_token
            sync_state.last_sync_at = datetime.now(timezone.utc)
        else:
            db.add(
                SyncState(
                    user_id=user_id,
                    folder=folder_key,
                    delta_token=new_token,
                    last_sync_at=datetime.now(timezone.utc),
                )
            )
        db.flush()

    try:
        db.commit()
    except Exception as e:
        logger.warning("Calendar activities commit failed: {}", e)
        db.rollback()

    logger.info(
        "Calendar delta scan [user {}]: {} fetched, {} changed, {} removed{}",
        user_id,
        len(events),
        events_changed,
        events_removed,
        " (delta token reset after 410)" if token_reset else "",
    )

    return {
        "events_scanned": events_changed,
        "vendor_meetings": vendor_meetings,
        "trade_shows": trade_shows,
        "activities_logged": activities_logged,
        "events_removed": events_removed,
    }


def _remove_calendar_activity(db: Session, graph_event_id: str) -> int:
    """Delete local ActivityLog rows for a calendar event removed upstream.

    Handles ``@removed`` delta entries (event deleted / cancelled in Outlook) by
    deleting every ActivityLog whose ``external_id`` matches the event's dedupe
    key.  Returns the number of rows deleted.

    Called by: scan_calendar_events (incremental delta path).
    """
    from app.models import ActivityLog

    external_id = f"calendar-{graph_event_id}"
    rows = db.scalars(select(ActivityLog).where(ActivityLog.external_id == external_id)).all()
    for row in rows:
        db.delete(row)
    if rows:
        logger.info("Calendar event removed upstream: deleted {} local row(s) for {}", len(rows), graph_event_id)
    return len(rows)


def _log_calendar_activity(
    db: Session,
    user_id: int | None,
    graph_event_id: str,
    subject: str,
    start_dt: datetime,
    end_dt: datetime,
    organizer_email: str | None,
    attendee_emails: list[str],
    location: str | None,
) -> list:
    """Create ActivityLog entries for a calendar event via log_meeting_activity.

    For each event detected as a vendor-meeting, trade-show, or has-external-attendee
    the old pre-WS3 guarantee is preserved: the event is always recorded on the rep's
    feed even when no attendee resolves to a known CRM entity.  When log_meeting_activity
    returns linked rows those are used as-is.  When it returns [] because no attendee
    matched (not because the event was already deduped), exactly ONE unlinked
    ActivityType.MEETING fallback row is written so the meeting still appears on the
    timeline and activities_logged counts it.

    Returns the list of rows created.  Returns [] only when the event was already
    logged on a previous scan (idempotent dedup).
    """
    from app.models import ActivityLog
    from app.services.activity_service import log_meeting_activity

    rows = log_meeting_activity(
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

    if rows:
        return rows

    # log_meeting_activity returns [] for two reasons:
    #   (a) dedup — the external_id row already exists → nothing to do.
    #   (b) no CRM match — no attendee resolved → write one unlinked fallback.
    # Distinguish by checking whether the row is already present.
    external_id = f"calendar-{graph_event_id}"
    already_exists = db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
    if already_exists:
        return []

    # No match and not yet logged — write the unlinked fallback so the event is
    # captured on the rep's activity feed.
    from app.constants import ActivityType, Channel, Direction, EventType
    from app.services.cadence_service import bump_clocks_from_activity

    organizer_lower = (organizer_email or "").strip().lower()
    # Determine direction from whether the organizer is an internal domain address.
    # Without settings access here we treat an absent organizer as own-organised.
    from app.config import settings

    organizer_is_own = (not organizer_lower) or (organizer_lower.split("@")[-1] in settings.own_domains)
    direction = Direction.OUTBOUND if organizer_is_own else Direction.INBOUND
    duration_seconds = max(0, int((end_dt - start_dt).total_seconds()))

    record = ActivityLog(
        user_id=user_id,
        activity_type=ActivityType.MEETING,
        channel=Channel.CALENDAR,
        company_id=None,
        vendor_card_id=None,
        site_contact_id=None,
        subject=(subject or "")[:500] or None,
        external_id=external_id,
        direction=direction,
        event_type=EventType.MEETING,
        is_meaningful=True,
        duration_seconds=duration_seconds,
        occurred_at=start_dt,
        details={
            "attendees": attendee_emails[:20],
            "organizer": organizer_email,
            "location": location,
            "subject": subject,
            "graph_event_id": graph_event_id,
        },
        summary=f"Meeting: {subject or '(no subject)'}",
    )
    db.add(record)
    db.flush()
    bump_clocks_from_activity(db, record)
    logger.info(
        "Meeting activity logged (unlinked fallback): {} by user {}",
        graph_event_id,
        user_id,
    )
    return [record]
