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

import re
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy.orm import Session

BASE_URL = "https://api.8x8.com/analytics/work/v1"


# ═══════════════════════════════════════════════════════════════════════
#  PHONE NORMALIZATION & REVERSE LOOKUP
# ═══════════════════════════════════════════════════════════════════════


def normalize_phone(phone: str) -> str:
    """Strip a phone number to bare digits, removing +1 country code.

    Removes spaces, dashes, parens, dots, and leading +1.
    Returns last 10 digits (US number) or full digits if shorter.

    Called by: reverse_lookup_phone(), CDR processing
    Depends on: nothing
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    # Strip leading country code "1" if 11 digits
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def reverse_lookup_phone(phone: str, db: Session) -> dict | None:
    """Look up a phone number against CRM entities and return match context.

    Searches SiteContact, Company, and VendorCard in priority order.
    Returns dict with entity_type, entity_id, company_id, company_name,
    contact_name (if applicable), or None if no match.

    Called by: app/jobs/eight_by_eight_jobs.py (CDR processing)
    Depends on: app/models (SiteContact, Company, VendorCard, CustomerSite)
    """
    from ..models import Company, CustomerSite, SiteContact, VendorCard

    normalized = normalize_phone(phone)
    if len(normalized) < 7:
        return None

    # 1. Check SiteContact phone field
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.phone.isnot(None),
            SiteContact.is_active.is_(True),
        )
        .all()
    )
    for contact in contacts:
        if normalize_phone(contact.phone) == normalized:
            # Get company via customer_site
            site = db.get(CustomerSite, contact.customer_site_id)
            company_id = site.company_id if site else None
            company = db.get(Company, company_id) if company_id else None
            return {
                "entity_type": "contact",
                "entity_id": contact.id,
                "company_id": company_id,
                "company_name": company.name if company else None,
                "contact_name": contact.full_name,
                "site_id": site.id if site else None,
            }

    # 2. Check Company phone field
    companies = (
        db.query(Company)
        .filter(
            Company.phone.isnot(None),
            Company.is_active.is_(True),
        )
        .all()
    )
    for company in companies:
        if normalize_phone(company.phone) == normalized:
            return {
                "entity_type": "company",
                "entity_id": company.id,
                "company_id": company.id,
                "company_name": company.name,
                "contact_name": None,
                "site_id": None,
            }

    # 3. Check VendorCard phones (JSON list)
    vendors = (
        db.query(VendorCard)
        .filter(
            VendorCard.is_blacklisted.is_(False),
        )
        .all()
    )
    for vendor in vendors:
        vendor_phones = vendor.phones or []
        for vp in vendor_phones:
            if normalize_phone(str(vp)) == normalized:
                return {
                    "entity_type": "vendor",
                    "entity_id": vendor.id,
                    "company_id": None,
                    "company_name": vendor.display_name,
                    "contact_name": None,
                    "vendor_card_id": vendor.id,
                }

    logger.debug(f"reverse_lookup_phone: no match for {normalized}")
    return None


def get_extension_map(token: str, settings) -> dict[str, str]:
    """Fetch 8x8 user list and build extension-to-email mapping.

    Calls 8x8 user list API to map internal extensions to user emails.
    Returns dict like {"1001": "michael@trio.com", "1002": "marcus@trio.com"}.

    Called by: app/jobs/eight_by_eight_jobs.py (CDR processing)
    Depends on: 8x8 Analytics API, httpx
    """
    url = f"{BASE_URL}/users"
    headers = {
        "Authorization": f"Bearer {token}",
        "8x8-apikey": settings.eight_by_eight_api_key,
    }

    try:
        resp = httpx.get(url, headers=headers, timeout=15)
    except httpx.HTTPError as e:
        logger.error(f"8x8 user list fetch failed: {e}")
        return {}

    if resp.status_code != 200:
        logger.warning(f"8x8 user list error: HTTP {resp.status_code}")
        return {}

    body = resp.json()
    users = body.get("data", [])
    ext_map = {}
    for user in users:
        ext = user.get("extension") or user.get("extensionNumber")
        email = user.get("email") or user.get("userId")
        if ext and email:
            ext_map[str(ext)] = str(email).lower()

    logger.info(f"8x8 extension map loaded: {len(ext_map)} extensions")
    return ext_map


def get_access_token(settings) -> str:
    """Authenticate with 8x8 Analytics API and return an access token.

    POST /oauth/token with API key header + form-encoded credentials. Raises ValueError
    on auth failure or missing token.
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
