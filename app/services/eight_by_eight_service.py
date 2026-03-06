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

    GET /call-records with pbxId, time window, and timezone.
    Filters out internal (ext-to-ext) calls before returning.
    Returns empty list on any error — never crashes.
    """
    url = f"{BASE_URL}/call-records"
    headers = {
        "Authorization": f"Bearer {token}",
        "8x8-apikey": settings.eight_by_eight_api_key,
    }
    params = {
        "pbxId": settings.eight_by_eight_pbx_id,
        "startTime": since.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": until.strftime("%Y-%m-%d %H:%M:%S"),
        "timeZone": settings.eight_by_eight_timezone,
        "pageSize": 200,
    }

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
    except httpx.HTTPError as e:
        logger.error(f"8x8 CDR fetch failed: {e}")
        return []

    if resp.status_code != 200:
        logger.error(f"8x8 CDR fetch error: HTTP {resp.status_code} — {resp.text[:200]}")
        return []

    body = resp.json()
    records = body if isinstance(body, list) else body.get("data", body.get("records", []))

    # Filter out internal ext-to-ext calls
    external = [r for r in records if r.get("direction", "").lower() != "internal"]
    logger.info(f"8x8 CDR fetch: {len(records)} total, {len(external)} external")
    return external


def normalize_cdr(cdr: dict) -> dict:
    """Map raw 8x8 CDR fields to AVAIL activity_log fields.

    Handles missing fields gracefully — returns defaults for any absent key.
    """
    # Parse startTime to datetime
    raw_time = cdr.get("startTime", "")
    occurred_at = None
    if raw_time:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                occurred_at = datetime.strptime(raw_time, fmt).replace(tzinfo=timezone.utc)
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

    return {
        "external_id": cdr.get("callId", ""),
        "occurred_at": occurred_at,
        "duration_seconds": duration_seconds,
        "caller_phone": cdr.get("caller", ""),
        "callee_phone": cdr.get("callee", ""),
        "caller_name": cdr.get("callerName", ""),
        "callee_name": cdr.get("calleeName", ""),
        "direction": cdr.get("direction", ""),
        "is_missed": cdr.get("missed", "") == "Missed",
    }
