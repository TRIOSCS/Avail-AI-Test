"""RocketReach client — contact lookup and company contact search.

API docs: https://rocketreach.co/api/docs
Gracefully returns empty results when API key is not configured.
"""

import asyncio
import logging

from app.config import settings
from app.http_client import http

log = logging.getLogger("avail.rocketreach")

ROCKETREACH_BASE = "https://api.rocketreach.co/api/v2"
_semaphore = asyncio.Semaphore(3)


async def search_company_contacts(
    company: str,
    domain: str | None = None,
    title_filter: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search for contacts at a company.

    Returns: [{
        full_name, title, email, phone, linkedin_url,
        company_name, source: "rocketreach", confidence
    }]
    """
    api_key = settings.rocketreach_api_key
    if not api_key or not company:
        return []

    query = [{"name": "current_employer", "value": company}]
    if title_filter:
        query.append({"name": "current_title", "value": title_filter})
    if domain:
        query.append({"name": "company_domain", "value": domain})

    async with _semaphore:
        try:
            resp = await http.post(
                f"{ROCKETREACH_BASE}/search",
                json={
                    "query": query,
                    "start": 1,
                    "page_size": min(limit, 25),
                },
                headers={
                    "Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                log.warning("RocketReach search failed: %s %s", resp.status_code, resp.text[:200])
                return []

            data = resp.json()
            profiles = data.get("profiles", [])

            contacts = []
            for p in profiles[:limit]:
                emails = p.get("emails", [])
                phones = p.get("phones", [])
                contacts.append({
                    "full_name": p.get("name"),
                    "title": p.get("current_title"),
                    "email": emails[0].get("email") if emails else None,
                    "phone": phones[0].get("number") if phones else None,
                    "linkedin_url": p.get("linkedin_url"),
                    "company_name": p.get("current_employer"),
                    "source": "rocketreach",
                    "confidence": 0.8 if emails else 0.5,
                })
            return contacts
        except Exception as e:
            log.warning("RocketReach search error: %s", e)
            return []
