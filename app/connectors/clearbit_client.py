"""Clearbit client — company and person enrichment.

API docs: https://clearbit.com/docs
Gracefully returns empty results when API key is not configured.
"""

import asyncio
import logging

from app.config import settings
from app.http_client import http

log = logging.getLogger("avail.clearbit")

_semaphore = asyncio.Semaphore(5)


async def enrich_company(domain: str) -> dict | None:
    """Enrich a company by domain — firmographic data.

    Returns: {
        legal_name, domain, industry, employee_size, hq_city, hq_state,
        hq_country, website, linkedin_url, description, tech_stack,
        revenue_range, founded_year, source: "clearbit"
    } or None.
    """
    api_key = settings.clearbit_api_key
    if not api_key or not domain:
        return None

    async with _semaphore:
        try:
            resp = await http.get(
                "https://company.clearbit.com/v2/companies/find",
                params={"domain": domain},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=20,
            )
            if resp.status_code == 202:
                # Async lookup — not ready yet
                log.debug("Clearbit company lookup queued for %s", domain)
                return None
            if resp.status_code != 200:
                log.warning("Clearbit company failed: %s %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            geo = data.get("geo", {}) or {}
            metrics = data.get("metrics", {}) or {}
            emp_range = metrics.get("employeesRange")

            return {
                "legal_name": data.get("legalName") or data.get("name"),
                "domain": data.get("domain", domain),
                "industry": data.get("category", {}).get("industry"),
                "employee_size": emp_range,
                "hq_city": geo.get("city"),
                "hq_state": geo.get("state"),
                "hq_country": geo.get("country"),
                "website": data.get("url"),
                "linkedin_url": data.get("linkedin", {}).get("handle"),
                "description": data.get("description"),
                "tech_stack": data.get("tech", []),
                "revenue_range": metrics.get("estimatedAnnualRevenue"),
                "founded_year": data.get("foundedYear"),
                "source": "clearbit",
            }
        except Exception as e:
            log.warning("Clearbit company error: %s", e)
            return None


