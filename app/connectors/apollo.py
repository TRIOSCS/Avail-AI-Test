"""Apollo.io API connector for company and contact enrichment.

Called by: app/enrichment_service.py (enrichment pipeline Phase 1b)
Depends on: app/config.py (apollo_api_key)
"""

import httpx
from loguru import logger

APOLLO_BASE = "https://api.apollo.io/v1"
_HEADERS_TEMPLATE = {"Content-Type": "application/json"}


def _headers(api_key: str) -> dict:
    """Build Apollo API request headers."""
    return {**_HEADERS_TEMPLATE, "X-Api-Key": api_key}


def _parse_company_response(data: dict) -> dict | None:
    """Parse Apollo company response into normalized format."""
    org = data.get("organization")
    if not org:
        return None
    return {
        "source": "apollo",
        "legal_name": org.get("name"),
        "domain": org.get("website_url", "").replace("https://", "").replace("http://", "").rstrip("/"),
        "linkedin_url": org.get("linkedin_url"),
        "industry": org.get("industry"),
        "employee_size": str(org.get("estimated_num_employees", "")) if org.get("estimated_num_employees") else None,
        "hq_city": org.get("city"),
        "hq_state": org.get("state"),
        "hq_country": org.get("country"),
    }


def _parse_contacts_response(data: dict) -> list[dict]:
    """Parse Apollo people search response into normalized contacts."""
    return [
        {
            "source": "apollo",
            "full_name": person.get("name"),
            "email": person.get("email"),
            "phone": person.get("phone_number"),
            "title": person.get("title"),
            "linkedin_url": person.get("linkedin_url"),
        }
        for person in data.get("people", [])
    ]


async def search_company(domain: str, api_key: str) -> dict | None:
    """Look up a company on Apollo by domain."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{APOLLO_BASE}/organizations/enrich",
                headers=_headers(api_key),
                params={"domain": domain},
            )
            if resp.status_code != 200:
                logger.warning("Apollo company lookup failed: %s", resp.status_code)
                return None
            return _parse_company_response(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error("Apollo company lookup error: %s", e)
        return None


async def search_contacts(domain: str, api_key: str, limit: int = 10) -> list[dict]:
    """Search for contacts at a company on Apollo."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{APOLLO_BASE}/mixed_people/search",
                headers=_headers(api_key),
                json={
                    "organization_domains": [domain],
                    "page": 1,
                    "per_page": limit,
                },
            )
            if resp.status_code != 200:
                logger.warning("Apollo contacts search failed: %s", resp.status_code)
                return []
            return _parse_contacts_response(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error("Apollo contacts search error: %s", e)
        return []
