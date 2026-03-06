"""eight_by_eight_service.py — 8x8 Work Analytics API client.

Fetches Call Detail Records (CDRs) from the 8x8 Analytics API
and returns normalized call data for activity logging.

Business Rules:
- Only fetch CDRs for users with eight_by_eight_enabled = True
- Use external_id (callId) for dedup — never log the same call twice
- Missed/abandoned calls are logged but duration_seconds = 0
- Internal ext-to-ext calls are excluded (external calls only)

Called by: app/jobs/eight_by_eight_jobs.py
Depends on: app/config.py, httpx
"""

from datetime import datetime, timezone

import httpx
from loguru import logger

BASE_URL = "https://api.8x8.com/analytics/work/v1"


def get_access_token(settings) -> str:
    """Authenticate with 8x8 Analytics API and return an access token.

    POST /oauth/token with API key header + form-encoded credentials.
    Raises ValueError on auth failure or missing token.
    """
    url = f"{BASE_URL}/oauth/token"
    headers = {
        "8x8-apikey": settings.eight_by_eight_api_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "username": settings.eight_by_eight_username,
        "password": settings.eight_by_eight_password,
    }

    try:
        resp = httpx.post(url, headers=headers, data=data, timeout=15)
    except httpx.HTTPError as e:
        logger.error(f"8x8 auth request failed: {e}")
        raise ValueError(f"8x8 auth request failed: {e}") from e

    if resp.status_code != 200:
        logger.error(f"8x8 auth failed: HTTP {resp.status_code} — {resp.text[:200]}")
        raise ValueError(f"8x8 auth failed: HTTP {resp.status_code}")

    body = resp.json()
    token = body.get("access_token") or body.get("token")
    if not token:
        logger.error(f"8x8 auth response missing token: {list(body.keys())}")
        raise ValueError("8x8 auth response missing access_token")

    logger.info("8x8 auth successful")
    return token


def get_cdrs(token: str, settings, since: datetime, until: datetime) -> list[dict]:
    """Fetch Call Detail Records from the 8x8 Analytics API.

    GET /v1/cdr with pbxId=allpbxes, isCallRecord=true, time window, and timezone.
    Paginates via scrollId. Returns empty list on any error — never crashes.
    """
    url = f"{BASE_URL}/cdr"
    headers = {
        "Authorization": f"Bearer {token}",
        "8x8-apikey": settings.eight_by_eight_api_key,
    }
    params = {
        "pbxId": "allpbxes",
        "startTime": since.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": until.strftime("%Y-%m-%d %H:%M:%S"),
        "timeZone": settings.eight_by_eight_timezone,
        "pageSize": 200,
        "isCallRecord": "true",
    }

    all_records = []
    scroll_id = None

    while True:
        if scroll_id:
            params["scrollId"] = scroll_id

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
        except httpx.HTTPError as e:
            logger.error(f"8x8 CDR fetch failed: {e}")
            return all_records

        if resp.status_code != 200:
            logger.error(f"8x8 CDR fetch error: HTTP {resp.status_code} — {resp.text[:200]}")
            return all_records

        body = resp.json()
        records = body.get("data", [])
        all_records.extend(records)

        # Check for more pages
        meta = body.get("meta", {})
        new_scroll = meta.get("scrollId")
        if not new_scroll or not records or len(all_records) >= meta.get("totalRecordCount", 0):
            break
        scroll_id = new_scroll

    logger.info(f"8x8 CDR fetch: {len(all_records)} call records")
    return all_records


def normalize_cdr(cdr: dict) -> dict:
    """Map raw 8x8 CDR fields to AVAIL activity_log fields.

    Handles missing fields gracefully — returns defaults for any absent key.
    Real 8x8 timestamps look like: "2026-03-05T16:43:13.502-0800"
    """
    # Parse startTimeUTC (epoch ms) first — most reliable
    occurred_at = None
    start_utc = cdr.get("startTimeUTC")
    if start_utc:
        try:
            occurred_at = datetime.fromtimestamp(int(start_utc) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    # Fallback: parse startTime string
    if occurred_at is None:
        raw_time = cdr.get("startTime", "")
        if raw_time:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
                try:
                    occurred_at = datetime.strptime(raw_time, fmt)
                    if occurred_at.tzinfo is None:
                        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc)

    # Duration: talkTimeMS (milliseconds) → seconds
    talk_ms = cdr.get("talkTimeMS", 0)
    try:
        duration_seconds = int(int(talk_ms) / 1000)
    except (TypeError, ValueError):
        duration_seconds = 0

    # Determine the internal extension (caller for outgoing, callee for incoming)
    direction = cdr.get("direction", "")
    if direction == "Outgoing":
        extension = cdr.get("caller", "")
    else:
        # For Incoming, the callee is the AA or extension that received the call
        extension = cdr.get("callee", "")

    return {
        "external_id": str(cdr.get("callId", "")),
        "occurred_at": occurred_at,
        "duration_seconds": duration_seconds,
        "caller_phone": cdr.get("caller", ""),
        "callee_phone": cdr.get("callee", ""),
        "caller_name": cdr.get("callerName", ""),
        "callee_name": cdr.get("calleeName", ""),
        "direction": direction,
        "is_missed": cdr.get("missed") == "Missed",
        "is_answered": cdr.get("answered") == "Answered",
        "extension": extension,
        "department": (cdr.get("departments") or [None])[0],
    }
