"""Apollo.io client — B2B contact enrichment.

Primary tier for contact discovery. Returns verified emails with
deliverability status when available.

API docs: https://apolloio.github.io/apollo-api-docs/

Gracefully returns empty results when API key is not configured.
"""
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("avail.apollo")

APOLLO_BASE = "https://api.apollo.io/api/v1"

# Default title keywords for electronic component sales
DEFAULT_TITLES = [
    "procurement", "purchasing", "buyer", "supply chain",
    "component engineer", "commodity manager",
    "materials manager", "sourcing",
]


async def search_contacts(
    company_name: str,
    domain: str | None = None,
    title_keywords: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search Apollo for contacts at a company.

    Returns: [{
        full_name, title, email, email_status (verified/guessed/unavailable),
        phone, linkedin_url, source: "apollo", confidence: "high"|"medium"
    }]

    Returns empty list if API key not configured or API fails.
    """
    api_key = getattr(settings, "apollo_api_key", "")
    if not api_key:
        return []

    titles = title_keywords or DEFAULT_TITLES

    # Build search payload
    payload: dict[str, Any] = {
        "api_key": api_key,
        "q_organization_name": company_name,
        "person_titles": titles,
        "per_page": min(limit, 25),
        "page": 1,
    }

    if domain:
        payload["q_organization_domains"] = domain

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{APOLLO_BASE}/mixed_people/search",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code != 200:
                log.warning(f"Apollo search failed: {resp.status_code} {resp.text[:200]}")
                return []

            data = resp.json()
            people = data.get("people", [])

            contacts = []
            for person in people[:limit]:
                email = person.get("email")
                email_status = person.get("email_status", "unavailable")

                # Confidence based on email verification
                if email and email_status == "verified":
                    confidence = "high"
                elif email:
                    confidence = "medium"
                else:
                    confidence = "low"

                contacts.append({
                    "full_name": _full_name(person),
                    "title": person.get("title") or person.get("headline"),
                    "email": email,
                    "email_status": email_status,
                    "phone": _best_phone(person),
                    "linkedin_url": person.get("linkedin_url"),
                    "source": "apollo",
                    "confidence": confidence,
                })

            return contacts

    except Exception as e:
        log.warning(f"Apollo API error: {e}")
        return []


async def enrich_single_contact(email: str) -> dict | None:
    """Enrich a single contact by email address.

    Returns enriched data or None.
    """
    api_key = getattr(settings, "apollo_api_key", "")
    if not api_key or not email:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{APOLLO_BASE}/people/match",
                json={
                    "api_key": api_key,
                    "email": email,
                },
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code != 200:
                return None

            person = resp.json().get("person")
            if not person:
                return None

            return {
                "full_name": _full_name(person),
                "title": person.get("title"),
                "email": person.get("email"),
                "email_status": person.get("email_status"),
                "phone": _best_phone(person),
                "linkedin_url": person.get("linkedin_url"),
                "company_name": person.get("organization", {}).get("name"),
                "source": "apollo",
            }

    except Exception as e:
        log.warning(f"Apollo enrich error: {e}")
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
