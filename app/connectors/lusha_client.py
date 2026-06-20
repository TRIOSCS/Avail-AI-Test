"""Lusha client — B2B contact + company enrichment.

What it does:
  - enrich_company(domain)  → firmographics for a company domain
  - search_contacts(...)    → decision-maker contacts at a company

Auth: the API key goes in the ``api_key`` request header. Base https://api.lusha.com.

Contacts use Lusha's v3 search → enrich pattern (the v2 company-by-domain GET and
contacts-by-domain search are being deprecated):
  1. POST /prospecting/contact/search  → requestId + contactIds (no contact data, free)
  2. POST /prospecting/contact/enrich  → full contacts incl. emails/phones

Each email entry carries a ``confidence`` field — that's the email-quality indicator.

Called by: app.enrichment_service (_lusha_find_company / _lusha_find_contacts),
           app.routers.sources (_LushaTestConnector).
Depends on: app.http_client (shared client), app.services.credential_service (key).

Gracefully returns empty/None when the key is not configured.
"""

import logging
from typing import Any

from app.connectors.resilience import resilient_call
from app.http_client import http
from app.services.credential_service import get_credential_cached

log = logging.getLogger("avail.lusha")

LUSHA_BASE = "https://api.lusha.com"

# Default title keywords for electronic component sourcing outreach.
DEFAULT_TITLES = [
    "procurement",
    "purchasing",
    "buyer",
    "supply chain",
    "component engineer",
    "commodity manager",
    "materials manager",
    "sourcing",
]


def _api_key() -> str | None:
    return get_credential_cached("lusha_enrichment", "LUSHA_API_KEY")


def _headers(key: str) -> dict:
    return {"api_key": key, "Content-Type": "application/json"}


def _email_confidence_label(marker) -> str:
    """Map a Lusha email ``confidence`` value to our high/medium/low scale."""
    if marker is None:
        return "medium"
    if isinstance(marker, (int, float)):
        if marker > 1:  # 0..100 scale
            return "high" if marker >= 80 else "medium" if marker >= 50 else "low"
        return "high" if marker >= 0.8 else "medium" if marker >= 0.5 else "low"
    m = str(marker).strip().lower()
    if m in ("a", "a+", "a1", "high", "verified", "valid"):
        return "high"
    if m in ("c", "low", "invalid"):
        return "low"
    return "medium"


def _best_email(emails: list) -> tuple[str | None, str]:
    """Pick the highest-confidence email. Returns (email, confidence_label)."""
    best = None
    best_label = "low"
    rank = {"high": 3, "medium": 2, "low": 1}
    for e in emails or []:
        if isinstance(e, dict):
            addr = e.get("email") or e.get("address") or e.get("emailAddress")
            label = _email_confidence_label(e.get("confidence") or e.get("emailConfidence"))
        else:
            addr, label = e, "medium"
        if not addr:
            continue
        if best is None or rank[label] > rank[best_label]:
            best, best_label = addr, label
    return best, best_label


def _best_phone(phones: list) -> str | None:
    for p in phones or []:
        if isinstance(p, dict):
            num = p.get("number") or p.get("phoneNumber") or p.get("internationalNumber")
            if num:
                return num
        elif p:
            return p
    return None


async def enrich_company(domain: str) -> dict | None:
    """Enrich a company by domain. Returns a Clearbit-shaped dict or None.

    Same output shape as apollo_client.enrich_company so the enrichment
    service can merge it uniformly.
    """
    key = _api_key()
    if not key or not domain:
        return None

    try:
        resp = await resilient_call(
            "lusha",
            lambda: http.get(
                f"{LUSHA_BASE}/company",
                params={"domain": domain},
                headers=_headers(key),
                timeout=15,
            ),
        )
        if resp.status_code != 200:
            log.warning("Lusha company lookup failed: %s %s", resp.status_code, resp.text[:200])
            return None

        body = resp.json()
        # Company payload may be at top level or under "data"/"company".
        data = body.get("data") or body.get("company") or body
        if not isinstance(data, dict) or not data:
            return None

        location = data.get("location") or {}
        if not isinstance(location, dict):
            location = {}

        social = data.get("social") or {}
        linkedin = None
        if isinstance(social, dict):
            linkedin = social.get("linkedin") or social.get("linkedin_url")

        return {
            "source": "lusha",
            "legal_name": data.get("name") or data.get("companyName"),
            "domain": data.get("domain") or domain,
            "industry": data.get("mainIndustry") or data.get("industry"),
            "employee_size": data.get("companySize") or data.get("employees"),
            "hq_city": location.get("city"),
            "hq_state": location.get("state") or location.get("region"),
            "hq_country": location.get("country") or location.get("countryName"),
            "website": data.get("website") or data.get("domain"),
            "linkedin_url": linkedin or data.get("linkedin_url"),
        }
    except Exception as e:
        log.warning("Lusha company enrich error: %s", e)
        return None


async def search_contacts(
    company_name: str = "",
    domain: str | None = None,
    title_keywords: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Find contacts at a company via Lusha's search → enrich flow.

    Returns a list of:
      {full_name, title, email, email_status, phone, linkedin_url,
       source: "lusha", confidence: "high"|"medium"|"low"}

    Empty list when the key is missing, the company can't be found, or the API fails.
    """
    key = _api_key()
    if not key or not domain:
        return []

    titles = title_keywords or DEFAULT_TITLES

    try:
        # Step 1 — search (returns contactIds + a requestId; no contact data).
        search_payload: dict[str, Any] = {
            "pages": {"page": 0, "size": min(limit, 25)},
            "filters": {
                "companies": {"include": {"domains": [domain]}},
                "contacts": {"include": {"jobTitles": titles}},
            },
        }
        search_resp = await resilient_call(
            "lusha",
            lambda: http.post(
                f"{LUSHA_BASE}/prospecting/contact/search",
                headers=_headers(key),
                json=search_payload,
                timeout=30,
            ),
        )
        if search_resp.status_code != 200:
            log.warning("Lusha search failed: %s %s", search_resp.status_code, search_resp.text[:200])
            return []

        sdata = search_resp.json()
        request_id = sdata.get("requestId") or sdata.get("request_id")
        rows = sdata.get("data") or sdata.get("contacts") or []
        contact_ids = [
            r.get("contactId") or r.get("id") for r in rows
            if isinstance(r, dict) and (r.get("contactId") or r.get("id"))
        ]
        if not request_id or not contact_ids:
            return []

        # Step 2 — enrich the matched contactIds to get emails/phones.
        enrich_resp = await resilient_call(
            "lusha",
            lambda: http.post(
                f"{LUSHA_BASE}/prospecting/contact/enrich",
                headers=_headers(key),
                json={"requestId": request_id, "contactIds": contact_ids[:limit]},
                timeout=30,
            ),
        )
        if enrich_resp.status_code != 200:
            log.warning("Lusha enrich failed: %s %s", enrich_resp.status_code, enrich_resp.text[:200])
            return []

        edata = enrich_resp.json()
        enriched = edata.get("contacts") or edata.get("data") or []

        contacts = []
        for item in enriched:
            if not isinstance(item, dict):
                continue
            # Contact fields may be nested under "data".
            c = item.get("data") if isinstance(item.get("data"), dict) else item
            name = c.get("name") or c.get("fullName")
            if not name:
                first = (c.get("firstName") or "").strip()
                last = (c.get("lastName") or "").strip()
                name = (f"{first} {last}").strip() or None
            if not name:
                continue

            email, conf_label = _best_email(
                c.get("emailAddresses") or c.get("emails") or []
            )
            phone = _best_phone(c.get("phoneNumbers") or c.get("phones") or [])

            company = c.get("company") or {}
            linkedin = c.get("linkedinUrl") or c.get("linkedin_url")
            if not linkedin and isinstance(company, dict):
                linkedin = company.get("linkedin_url")

            contacts.append(
                {
                    "full_name": name,
                    "title": c.get("jobTitle") or c.get("title"),
                    "email": (email or "").lower() or None,
                    "email_status": conf_label if email else "unavailable",
                    "phone": phone,
                    "linkedin_url": linkedin,
                    "source": "lusha",
                    "confidence": conf_label if email else "low",
                }
            )
        return contacts
    except Exception as e:
        log.warning("Lusha contact search error: %s", e)
        return []
