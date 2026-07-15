"""Explorium API connector (real v1 pipeline: match → firmographics; prospects →
contacts).

Auth is the `api_key:` header (NOT Authorization: Bearer). Company enrichment is a
2-call pipeline (match to a business_id, then firmographics/enrich). 402/403/429 →
ProviderQuotaError; other errors degrade to None/[].

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py (http),
app/services/enrichment_credit_guard (ProviderQuotaError).
"""

import httpx
from loguru import logger

from app.http_client import http
from app.services.enrichment_credit_guard import ProviderQuotaError

BASE = "https://api.explorium.ai/v1"
_QUOTA_STATUSES = (402, 403, 429)


def _headers(api_key: str) -> dict:
    return {"api_key": api_key, "Content-Type": "application/json"}


def _data(resp) -> dict:
    """Extract the `data` envelope from a response, falling back to the full body."""
    body = resp.json() if resp is not None else {}
    return body.get("data") if isinstance(body.get("data"), dict) else body


def _fmt_band(obj) -> str | None:
    """Format a {min, max} range dict as 'min-max', 'min', or 'max'."""
    if isinstance(obj, dict):
        lo, hi = obj.get("min"), obj.get("max")
        if lo is not None and hi is not None:
            return f"{lo}-{hi}"
        return str(lo if lo is not None else hi) if (lo is not None or hi is not None) else None
    return str(obj) if obj else None


async def _post(path: str, api_key: str, body: dict):
    """POST to the Explorium API; raises ProviderQuotaError on 402/403/429.

    Returns the response object on success (200), or None on other non-quota errors.
    ProviderQuotaError is deliberately NOT caught here so callers can propagate it.
    """
    resp = await http.post(f"{BASE}{path}", headers=_headers(api_key), json=body, timeout=20)
    if resp.status_code in _QUOTA_STATUSES:
        raise ProviderQuotaError(f"Explorium {path} quota/limit: {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("Explorium {} failed: {}", path, resp.status_code)
        return None
    return resp


async def discover_businesses(filters: dict, size: int, api_key: str) -> list[dict]:
    """Discover businesses matching ICP filters via Explorium's documented Fetch
    Businesses endpoint (``POST /v1/businesses``).

    Uses the same verified machinery as ``enrich_company`` / ``search_contacts``: the
    ``api_key`` header, a ``{"filters": {...}, "size": N}`` request body, and a ``data``
    array response. 402/403/429 → ProviderQuotaError (propagated to the caller).
    Transport/parse errors → ``[]``. No results → ``[]``.
    """
    try:
        resp = await _post(
            "/businesses",
            api_key,
            {"mode": "full", "size": size, "filters": filters},
        )
        rows = (resp.json().get("data") if resp else None) or []
        return rows if isinstance(rows, list) else []
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Explorium discover_businesses error: {}", e)
        return []


async def _match_business_id(domain: str, name: str, api_key: str) -> str | None:
    """Call /businesses/match and return the first matched business_id, or None."""
    resp = await _post(
        "/businesses/match",
        api_key,
        {"businesses_to_match": [{"name": name, "domain": domain}]},
    )
    matched = (_data(resp) or {}).get("matched_businesses") or []
    business_id: str | None = (matched[0].get("business_id") if matched else None) or None  # Explorium JSON boundary
    return business_id


async def enrich_company(domain: str, name: str, api_key: str) -> dict | None:
    """Enrich a company via Explorium's 2-call pipeline (match → firmographics).

    402/403/429 → ProviderQuotaError (propagated to caller). Transport/parse errors →
    None. No match → None.
    """
    try:
        bid = await _match_business_id(domain, name, api_key)
        if not bid:
            return None
        resp = await _post("/businesses/firmographics/enrich", api_key, {"business_id": bid})
        f = _data(resp) or {}
        if not f:
            return None
        out = {
            "source": "explorium",
            "legal_name": f.get("name"),
            "domain": (f.get("website") or domain).replace("https://", "").replace("http://", "").split("/")[0],
            "website": f.get("website"),
            "industry": f.get("linkedin_industry_category"),
            "employee_size": _fmt_band(f.get("number_of_employees_range")),
            "hq_city": f.get("city_name"),
            "hq_state": f.get("region_name"),
            "hq_country": f.get("country_name"),
            "linkedin_url": f.get("linkedin_profile"),
            "naics": f.get("naics"),
            "ticker": f.get("ticker"),
            "revenue_range": _fmt_band(f.get("yearly_revenue_range")),
        }
        return out if any(v for k, v in out.items() if k not in ("source", "domain")) else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Explorium company error: {}", e)
        return None


async def search_contacts(
    domain: str,
    name: str,
    api_key: str,
    title_filter: str,
    limit: int,
) -> list[dict]:
    """Search contacts at a company via Explorium's prospects pipeline.

    402/403/429 → ProviderQuotaError (propagated to caller). Transport/parse errors →
    []. No match → [].
    """
    try:
        bid = await _match_business_id(domain, name, api_key)
        if not bid:
            return []
        filters: dict = {"business_id": {"values": [bid]}, "has_email": True}
        if title_filter:
            filters["job_title"] = {"values": [title_filter]}
        resp = await _post("/prospects", api_key, {"filters": filters, "size": limit})
        rows = (resp.json().get("data") if resp else None) or []
        contacts: list[dict] = []
        for p in rows[:limit]:
            pid = p.get("prospect_id")
            ci_resp = (
                await _post(
                    "/prospects/contacts_information/enrich",
                    api_key,
                    {"prospect_id": pid},
                )
                if pid
                else None
            )
            ci = _data(ci_resp) or {}
            contacts.append(
                {
                    "source": "explorium",
                    "full_name": p.get("full_name"),
                    "title": p.get("job_title"),
                    "linkedin_url": p.get("linkedin"),
                    "location": p.get("city") or p.get("region_name"),
                    "company": p.get("company_name"),
                    "email": ci.get("professional_email"),
                    "phone": ci.get("mobile_phone") or ((ci.get("phone_numbers") or [None])[0]),
                    "verified": (ci.get("professional_email_status") == "valid"),
                }
            )
        return [c for c in contacts if c.get("full_name")]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Explorium contacts error: {}", e)
        return []
