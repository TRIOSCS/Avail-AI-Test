"""Calendar availability service — check buyer availability for routing.

Checks Microsoft Graph calendar API for Out of Office, PTO, and vacation events.
Simple binary check: "is this buyer available today?"

Business Rules:
- Unavailable if: showAs == "oof", or all-day "busy" event,
  or event title contains PTO/vacation/OOO
- Results cached per user per day (only one API call per user per day)
- Graceful degradation: if API fails → assume buyer IS available
  (never block routing because of API hiccup)

Called by: services/routing_service.py
Depends on: utils/graph_client.py, scheduler.py (get_valid_token)
"""

import logging
from datetime import date

log = logging.getLogger("avail.calendar")

# Cache: { "user_id:YYYY-MM-DD" → bool (True=available) }
_availability_cache: dict[str, bool] = {}

# Keywords in event titles that indicate unavailability
_OOO_KEYWORDS = {"pto", "vacation", "ooo", "out of office", "holiday", "sick", "leave"}


async def is_buyer_available(user_id: int, check_date: date, db) -> bool:
    """Check if a buyer is available on a given date.

    Returns True if available, False if out of office.
    On any error, returns True (graceful degradation — never block routing).
    """
    cache_key = f"{user_id}:{check_date.isoformat()}"
    if cache_key in _availability_cache:
        return _availability_cache[cache_key]

    try:
        available = await _check_calendar(user_id, check_date, db)
        _availability_cache[cache_key] = available
        return available
    except Exception as e:
        log.warning(f"Calendar check failed for user {user_id}: {e} — assuming available")
        _availability_cache[cache_key] = True
        return True


async def _check_calendar(user_id: int, check_date: date, db) -> bool:
    """Query Graph API calendarView for the given user and date.

    Returns True if available, False if OOO/PTO detected.
    """
    from app.models import User
    from app.scheduler import get_valid_token
    from app.utils.graph_client import GraphClient

    user = db.get(User, user_id)
    if not user or not user.access_token:
        return True  # No token → assume available

    token = await get_valid_token(user, db)
    if not token:
        return True

    gc = GraphClient(token)

    start = f"{check_date.isoformat()}T00:00:00Z"
    end = f"{check_date.isoformat()}T23:59:59Z"

    result = await gc.get_json(
        "/me/calendarView",
        params={
            "startDateTime": start,
            "endDateTime": end,
            "$select": "subject,isAllDay,showAs,start,end",
            "$top": "50",
        },
    )

    if "error" in result:
        log.debug(f"Calendar API error for user {user_id}: {result}")
        return True  # Graceful degradation

    events = result.get("value", [])

    for event in events:
        show_as = (event.get("showAs") or "").lower()
        subject = (event.get("subject") or "").lower()
        is_all_day = event.get("isAllDay", False)

        # Out of Office status
        if show_as == "oof":
            log.info(f"User {user_id} is OOO on {check_date}: '{event.get('subject')}'")
            return False

        # All-day busy event
        if is_all_day and show_as == "busy":
            log.info(f"User {user_id} has all-day busy event on {check_date}: '{event.get('subject')}'")
            return False

        # PTO/vacation keywords in title
        if any(kw in subject for kw in _OOO_KEYWORDS):
            log.info(f"User {user_id} has PTO/OOO event on {check_date}: '{event.get('subject')}'")
            return False

    return True


def clear_cache():
    """Clear availability cache (for testing or daily reset)."""
    _availability_cache.clear()
