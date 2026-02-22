"""Apollo.io client — B2B contact enrichment.

Two capabilities:
  1. search_contacts() — find contacts at a company by title (mixed_people/search)
  2. enrich_person()   — enrich a known person by name+domain/email (people/match)

API docs: https://docs.apollo.io/reference/people-enrichment

Gracefully returns empty results when API key is not configured.
"""

import logging
from typing import Any

from app.config import settings
from app.http_client import http

log = logging.getLogger("avail.apollo")

APOLLO_BASE = "https://api.apollo.io/api/v1"

# Default title keywords for electronic component sales
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


async def search_contacts(
    company_name: str,
    domain: str | None = None,
    title_keywords: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search Apollo for contacts at a company, then enrich top results.

    Step 1: api_search finds people by title (free, no emails)
    Step 2: people/match enriches each to get email + phone

    Returns: [{
        full_name, title, email, email_status, phone, linkedin_url,
        source: "apollo", confidence: "high"|"medium"|"low"
    }]

    Returns empty list if API key not configured or API fails.
    """
    api_key = getattr(settings, "apollo_api_key", "")
    if not api_key:
        return []

    titles = title_keywords or DEFAULT_TITLES

    # Build search payload
    payload: dict[str, Any] = {
        "q_organization_name": company_name,
        "person_titles": titles,
        "per_page": min(limit, 25),
        "page": 1,
    }

    if domain:
        payload["q_organization_domains"] = domain

    try:
        resp = await http.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            log.warning(
                f"Apollo search failed: {resp.status_code} {resp.text[:200]}"
            )
            return []

        data = resp.json()
        people = data.get("people", [])

        # Enrich each person with people/match to get emails
        contacts = []
        for person in people[:limit]:
            first = (person.get("first_name") or "").strip()
            last = (person.get("last_name") or "").strip()
            linkedin = person.get("linkedin_url")
            pid = person.get("id")

            # Try enrichment via people/match (Apollo ID is most reliable)
            enriched = await enrich_person(
                apollo_id=pid,
                first_name=first or None,
                last_name=last or None,
                domain=domain,
                organization_name=company_name,
                linkedin_url=linkedin,
            )

            if enriched:
                contacts.append(enriched)
            else:
                # Fallback: return what search gave us (no email)
                contacts.append(
                    {
                        "full_name": _full_name(person),
                        "title": person.get("title") or person.get("headline"),
                        "email": None,
                        "email_status": "unavailable",
                        "phone": None,
                        "linkedin_url": linkedin,
                        "source": "apollo",
                        "confidence": "low",
                    }
                )

        return contacts

    except Exception as e:
        log.warning(f"Apollo API error: {e}")
        return []


async def enrich_person(
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    name: str | None = None,
    email: str | None = None,
    domain: str | None = None,
    organization_name: str | None = None,
    linkedin_url: str | None = None,
    apollo_id: str | None = None,
) -> dict | None:
    """Enrich a person via Apollo people/match.

    Provide at least one identifier (name+domain, email, or linkedin_url).
    Returns enriched profile dict or None on failure.

    Response keys: full_name, first_name, last_name, title, email,
    email_status, phone, linkedin_url, city, state, country,
    organization (name, domain, industry, size, founded_year),
    source: "apollo", confidence.
    """
    api_key = getattr(settings, "apollo_api_key", "")
    if not api_key:
        return None

    payload: dict[str, Any] = {}
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if name:
        payload["name"] = name
    if email:
        payload["email"] = email
    if domain:
        payload["domain"] = domain
    if organization_name:
        payload["organization_name"] = organization_name
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    if apollo_id:
        payload["id"] = apollo_id

    if not payload:
        return None

    try:
        resp = await http.post(
            f"{APOLLO_BASE}/people/match",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            log.warning(f"Apollo enrich failed: {resp.status_code} {resp.text[:200]}")
            return None

        data = resp.json()
        person = data.get("person")
        if not person:
            return None

        email_val = person.get("email")
        email_status = person.get("email_status", "unavailable")
        if email_val and email_status == "verified":
            confidence = "high"
        elif email_val:
            confidence = "medium"
        else:
            confidence = "low"

        org = person.get("organization") or {}
        return {
            "full_name": _full_name(person),
            "first_name": (person.get("first_name") or "").strip(),
            "last_name": (person.get("last_name") or "").strip(),
            "title": person.get("title") or person.get("headline"),
            "email": email_val,
            "email_status": email_status,
            "phone": _best_phone(person),
            "linkedin_url": person.get("linkedin_url"),
            "city": person.get("city"),
            "state": person.get("state"),
            "country": person.get("country"),
            "organization": {
                "name": org.get("name"),
                "domain": org.get("primary_domain") or org.get("website_url"),
                "industry": org.get("industry"),
                "size": org.get("estimated_num_employees"),
                "founded_year": org.get("founded_year"),
            },
            "source": "apollo",
            "confidence": confidence,
        }

    except Exception as e:
        log.warning(f"Apollo enrich error: {e}")
        return None


async def enrich_company(domain: str) -> dict | None:
    """Enrich a company via Apollo organizations/enrich endpoint.

    Returns a dict matching the enrichment service format (same shape as
    Clearbit) so apply_enrichment_to_vendor() works unchanged, or None on failure.
    """
    api_key = getattr(settings, "apollo_api_key", "")
    if not api_key:
        return None

    try:
        resp = await http.get(
            f"{APOLLO_BASE}/organizations/enrich",
            params={"domain": domain},
            headers={"X-Api-Key": api_key},
            timeout=15,
        )

        if resp.status_code != 200:
            log.warning("Apollo company enrich failed: %s %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        org = data.get("organization")
        if not org:
            return None

        # Build location fields
        city = org.get("city")
        state = org.get("state")
        country = org.get("country")

        return {
            "source": "apollo",
            "legal_name": org.get("name"),
            "domain": org.get("primary_domain") or domain,
            "industry": org.get("industry"),
            "employee_size": org.get("estimated_num_employees"),
            "hq_city": city,
            "hq_state": state,
            "hq_country": country,
            "website": org.get("website_url"),
            "linkedin_url": org.get("linkedin_url"),
            "founded_year": org.get("founded_year"),
        }

    except Exception as e:
        log.warning("Apollo company enrich error: %s", e)
        return None


def _full_name(person: dict) -> str:
    """Extract full name from Apollo person record."""
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    return person.get("name") or first or last or "Unknown"


def _best_phone(person: dict) -> str | None:
    """Extract best phone number from Apollo person record."""
    # Direct phone
    if person.get("phone_number"):
        return person["phone_number"]

    # Phone numbers array
    phones = person.get("phone_numbers", [])
    if phones:
        # Prefer direct dial > mobile > work
        for ptype in ("direct_dial", "mobile", "work"):
            for p in phones:
                if p.get("type") == ptype and p.get("sanitized_number"):
                    return p["sanitized_number"]
        # Fallback to first available
        if phones[0].get("sanitized_number"):
            return phones[0]["sanitized_number"]

    return None
