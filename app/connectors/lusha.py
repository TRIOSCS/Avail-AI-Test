"""Lusha API connector for company + contact enrichment (Lusha API v2).

Mirrors apollo.py but uses the shared app/http_client.py `http` singleton (connection
pooling) instead of a per-call httpx.AsyncClient. On HTTP 402/429 (quota/rate-limit)
raises ProviderQuotaError so the caller trips the cooldown circuit; other transport/parse
errors degrade to None / [].

Called by: app/enrichment_service.py (enrich_entity Phase 1a-Lusha, find_suggested_contacts).
Depends on: app/http_client.py (http), app/services/enrichment_credit_guard.py
            (ProviderQuotaError).
"""

import httpx
from loguru import logger

from app.http_client import http
from app.services.enrichment_credit_guard import ProviderQuotaError

LUSHA_BASE = "https://api.lusha.com/v2"
_QUOTA_STATUSES = (402, 429)


def _headers(api_key: str) -> dict:
    """Build Lusha v2 request headers (same scheme as the Lusha test connector)."""
    return {"api_key": api_key, "Content-Type": "application/json"}


def _parse_company(data: dict) -> dict | None:
    """Map a Lusha company payload to the shared firmographic shape, or None if
    empty."""
    org = data.get("data") or data.get("company") or {}
    if not org:
        return None
    location = org.get("location") or {}
    social = org.get("social") or {}
    out = {
        "source": "lusha",
        "legal_name": org.get("name") or org.get("legalName"),
        "domain": org.get("domain") or org.get("website"),
        "industry": org.get("industry"),
        "employee_size": org.get("employees") or org.get("employeeRange") or org.get("size"),
        "hq_city": location.get("city"),
        "hq_state": location.get("state"),
        "hq_country": location.get("country"),
        "linkedin_url": social.get("linkedin") or org.get("linkedinUrl"),
    }
    # Empty unless at least one informative field is present.
    if not any(v for k, v in out.items() if k not in ("source", "domain")):
        return None
    return out


def _parse_contacts(data: dict) -> list[dict]:
    """Map Lusha contact payloads to the shared contact shape (verified flag
    preserved)."""
    raw = data.get("contacts") or data.get("data") or []
    contacts = []
    for person in raw:
        name = person.get("fullName") or person.get("full_name") or person.get("name")
        if not name:
            continue
        emails = person.get("emailAddresses") or person.get("emails") or []
        phones = person.get("phoneNumbers") or person.get("phones") or []
        email = (emails[0].get("email") if emails and isinstance(emails[0], dict) else None) or person.get("email")
        phone = (phones[0].get("number") if phones and isinstance(phones[0], dict) else None) or person.get("phone")
        contacts.append(
            {
                "source": "lusha",
                "full_name": name,
                "email": email,
                "phone": phone,
                "title": person.get("jobTitle") or person.get("title"),
                "verified": bool(person.get("isEmailVerified") or person.get("verified")),
            }
        )
    return contacts


async def enrich_company(domain: str, api_key: str) -> dict | None:
    """Look up a company on Lusha by domain.

    402/429 → ProviderQuotaError; else None on error.
    """
    try:
        resp = await http.get(
            f"{LUSHA_BASE}/company",
            headers=_headers(api_key),
            params={"domain": domain},
            timeout=15,
        )
        if resp.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Lusha company quota/rate-limit: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Lusha company lookup failed: {}", resp.status_code)
            return None
        result = _parse_company(resp.json())
        if result is not None and not result.get("domain"):
            result["domain"] = domain
        return result
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Lusha company lookup error: {}", e)
        return None


async def search_contacts(domain: str, api_key: str, limit: int) -> list[dict]:
    """Search contacts at a company on Lusha.

    402/429 → ProviderQuotaError; else [] on error.
    """
    try:
        resp = await http.post(
            f"{LUSHA_BASE}/contacts",
            headers=_headers(api_key),
            json={"domain": domain, "limit": limit},
            timeout=20,
        )
        if resp.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Lusha contacts quota/rate-limit: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Lusha contacts search failed: {}", resp.status_code)
            return []
        return _parse_contacts(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Lusha contacts search error: {}", e)
        return []
