"""Apollo people search service — checks for procurement/supply chain staff.

Used after Explorium discovery to fill the "has procurement staff" signal
(worth 15 fit score points). Apollo free tier: 10,000 credits/month.
"""

import asyncio

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.apollo_client import APOLLO_BASE, DEFAULT_TITLES
from app.http_client import http
from app.models.prospect_account import ProspectAccount

APOLLO_RATE_LIMIT_PER_MIN = int(
    getattr(settings, "apollo_rate_limit_per_min", 5)
)

# Procurement-relevant title keywords
PROCUREMENT_TITLES = [
    "procurement",
    "supply chain",
    "purchasing",
    "commodity",
    "component engineer",
    "materials manager",
    "sourcing",
    "buyer",
]


def _get_api_key() -> str:
    return getattr(settings, "apollo_api_key", "")


async def check_people_signals(domain: str) -> dict:
    """Search Apollo for procurement/supply chain contacts at a domain.

    Returns:
        {has_procurement_staff: bool, contact_count: int, sample_contacts: list}
    """
    api_key = _get_api_key()
    if not api_key:
        return {"has_procurement_staff": None, "contact_count": 0, "sample_contacts": []}

    try:
        payload = {
            "q_organization_domains": domain,
            "person_titles": PROCUREMENT_TITLES,
            "per_page": 10,
            "page": 1,
        }

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
            logger.warning("Apollo people search failed for {}: {}", domain, resp.status_code)
            return {"has_procurement_staff": None, "contact_count": 0, "sample_contacts": []}

        data = resp.json()
        people = data.get("people", [])

        sample = []
        for p in people[:5]:
            sample.append({
                "name": _full_name(p),
                "title": p.get("title") or p.get("headline"),
                "email": p.get("email"),
                "linkedin_url": p.get("linkedin_url"),
                "seniority": p.get("seniority"),
            })

        return {
            "has_procurement_staff": len(people) > 0,
            "contact_count": len(people),
            "sample_contacts": sample,
        }

    except Exception as e:
        logger.error("Apollo people search error for {}: {}", domain, e)
        return {"has_procurement_staff": None, "contact_count": 0, "sample_contacts": []}


async def run_people_check_batch(
    prospect_ids: list[int], db: Session
) -> dict:
    """Check procurement staff for a batch of prospects.

    Rate-limited to APOLLO_RATE_LIMIT_PER_MIN requests/minute.

    Returns: {checked: int, has_staff: int, no_staff: int, errors: int}
    """
    if not _get_api_key():
        logger.warning("Apollo API key not configured — people check skipped")
        return {"checked": 0, "has_staff": 0, "no_staff": 0, "errors": 0}

    checked = 0
    has_staff = 0
    no_staff = 0
    errors = 0
    delay = 60.0 / APOLLO_RATE_LIMIT_PER_MIN  # seconds between requests

    for pid in prospect_ids:
        prospect = db.get(ProspectAccount, pid)
        if not prospect or not prospect.domain:
            continue

        try:
            result = await check_people_signals(prospect.domain)
            checked += 1

            # Update enrichment_data with people signals
            enrichment = prospect.enrichment_data or {}
            enrichment["apollo_people"] = result
            prospect.enrichment_data = enrichment

            # Update contacts_preview with sample contacts
            if result["sample_contacts"]:
                prospect.contacts_preview = result["sample_contacts"]

            if result["has_procurement_staff"] is True:
                has_staff += 1
            elif result["has_procurement_staff"] is False:
                no_staff += 1

            db.commit()

        except Exception as e:
            logger.error("People check failed for prospect {}: {}", pid, e)
            errors += 1
            db.rollback()

        # Rate limiting
        await asyncio.sleep(delay)

    logger.info(
        "Apollo people check: checked={}, has_staff={}, no_staff={}, errors={}",
        checked, has_staff, no_staff, errors,
    )

    return {
        "checked": checked,
        "has_staff": has_staff,
        "no_staff": no_staff,
        "errors": errors,
    }


async def enrich_company_apollo(domain: str) -> dict | None:
    """Enrich a company via Apollo organizations/enrich endpoint.

    Fallback enrichment source when Explorium doesn't find a company.
    Returns normalized dict or None.
    """
    api_key = _get_api_key()
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
            return None

        data = resp.json()
        org = data.get("organization")
        if not org:
            return None

        return {
            "name": org.get("name"),
            "domain": org.get("primary_domain") or domain,
            "website": org.get("website_url"),
            "industry": org.get("industry"),
            "employee_count_range": _format_size(org.get("estimated_num_employees")),
            "hq_location": ", ".join(filter(None, [
                org.get("city"), org.get("state"), org.get("country"),
            ])) or None,
            "region": _detect_region_from_country(org.get("country")),
            "description": org.get("short_description"),
            "discovery_source": "apollo",
        }

    except Exception as e:
        logger.warning("Apollo company enrich error for {}: {}", domain, e)
        return None


def _full_name(person: dict) -> str:
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last}"
    return person.get("name") or first or last or "Unknown"


def _format_size(count) -> str | None:
    if count is None:
        return None
    try:
        n = int(count)
    except (ValueError, TypeError):
        return str(count) if count else None
    if n <= 50:
        return "1-50"
    elif n <= 200:
        return "51-200"
    elif n <= 500:
        return "201-500"
    elif n <= 1000:
        return "501-1000"
    elif n <= 5000:
        return "1001-5000"
    elif n <= 10000:
        return "5001-10000"
    return "10001+"


def _detect_region_from_country(country: str | None) -> str | None:
    if not country:
        return None
    c = country.upper()
    if c in ("US", "USA", "UNITED STATES"):
        return "US"
    if c in ("GERMANY", "UK", "UNITED KINGDOM", "FRANCE", "NETHERLANDS", "SWEDEN",
             "ITALY", "SPAIN", "SWITZERLAND", "AUSTRIA", "BELGIUM"):
        return "EU"
    if c in ("CHINA", "JAPAN", "SOUTH KOREA", "TAIWAN", "SINGAPORE", "INDIA"):
        return "Asia"
    return None
