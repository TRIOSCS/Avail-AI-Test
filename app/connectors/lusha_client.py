"""Lusha client — direct dial phone number, email, and company discovery.

API docs: https://www.lusha.com/docs/api/v2/
Called by: enrichment_service.find_suggested_contacts(), customer_enrichment_service waterfall.
Depends on: app.config.settings.lusha_api_key, app.http_client.http
"""

import asyncio
from loguru import logger

from app.config import settings
from app.http_client import http


LUSHA_BASE = "https://api.lusha.com/v2"
_semaphore = asyncio.Semaphore(10)


def _best_phone(phones: list[dict]) -> tuple[str | None, str | None]:
    """Pick the best phone from Lusha's phone list.

    Priority: direct_dial > mobile > work > other.
    Returns (number, type) or (None, None).
    """
    if not phones:
        return None, None
    priority = {"direct_dial": 0, "mobile": 1, "work": 2}
    ranked = sorted(phones, key=lambda p: priority.get(p.get("type", ""), 99))
    best = ranked[0]
    return best.get("number"), best.get("type")


def _best_email(emails: list[dict]) -> str | None:
    """Pick the best email from Lusha's email list.

    Priority: work > personal > other.
    Returns email string or None.
    """
    if not emails:
        return None
    priority = {"work": 0, "personal": 1}
    ranked = sorted(emails, key=lambda e: priority.get(e.get("type", ""), 99))
    return ranked[0].get("email")


async def find_person(
    *,
    email: str | None = None,
    linkedin_url: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    company_name: str | None = None,
    company_domain: str | None = None,
) -> dict | None:
    """Look up a person via Lusha Person API v2.

    Accepts email, linkedin_url, or first_name+last_name with company_name/domain.
    Returns: {
        full_name, title, email, phone, phone_type, do_not_call,
        linkedin_url, location, source: "lusha", confidence
    } or None.
    """
    api_key = settings.lusha_api_key
    if not api_key:
        return None

    params: dict[str, str] = {}
    if email:
        params["email"] = email
    elif linkedin_url:
        params["linkedinUrl"] = linkedin_url
    elif first_name and last_name and (company_name or company_domain):
        params["firstName"] = first_name
        params["lastName"] = last_name
        if company_name:
            params["company"] = company_name
        if company_domain:
            params["domain"] = company_domain
    elif company_domain:
        params["domain"] = company_domain
    else:
        return None

    async with _semaphore:
        try:
            resp = await http.get(
                f"{LUSHA_BASE}/person",
                params=params,
                headers={"api_key": api_key},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Lusha person lookup failed: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None

            data = resp.json()
            first = (data.get("firstName") or "").strip()
            last = (data.get("lastName") or "").strip()
            full_name = f"{first} {last}".strip() if first or last else None

            phones = data.get("phoneNumbers") or []
            phone, phone_type = _best_phone(phones)

            emails = data.get("emailAddresses") or []
            best_email = _best_email(emails)

            return {
                "full_name": full_name,
                "title": data.get("title"),
                "email": best_email,
                "phone": phone,
                "phone_type": phone_type,
                "do_not_call": data.get("doNotCall", False),
                "linkedin_url": data.get("linkedinUrl"),
                "location": data.get("location"),
                "source": "lusha",
                "confidence": data.get("confidence", 0),
            }
        except Exception as e:
            logger.warning("Lusha person lookup error: %s", e)
            return None


async def search_contacts(
    company_domain: str,
    titles: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search for contacts at a company domain via Lusha.

    Filters by title keywords if provided (buyer, procurement, etc.).
    Returns list of contact dicts matching find_person output format.
    """
    api_key = settings.lusha_api_key
    if not api_key or not company_domain:
        return []

    title_set = {t.lower() for t in (titles or [])}

    async with _semaphore:
        try:
            params = {"domain": company_domain, "limit": min(limit * 3, 50)}
            resp = await http.get(
                f"{LUSHA_BASE}/company/contacts",
                params=params,
                headers={"api_key": api_key},
                timeout=25,
            )
            if resp.status_code != 200:
                logger.warning("Lusha contacts search failed: %s %s", resp.status_code, resp.text[:200])
                return []

            data = resp.json()
            contacts_raw = data.get("contacts") or data.get("data") or []
            contacts = []
            for c in contacts_raw:
                first = (c.get("firstName") or "").strip()
                last = (c.get("lastName") or "").strip()
                full_name = f"{first} {last}".strip() if first or last else None
                title = c.get("title") or ""
                # Filter by title keywords locally if provided
                if title_set and not any(kw in title.lower() for kw in title_set):
                    continue
                phones = c.get("phoneNumbers") or []
                phone, phone_type = _best_phone(phones)
                emails = c.get("emailAddresses") or []
                best_email = _best_email(emails)
                contacts.append({
                    "full_name": full_name,
                    "title": title or None,
                    "email": best_email,
                    "phone": phone,
                    "phone_type": phone_type,
                    "linkedin_url": c.get("linkedinUrl"),
                    "source": "lusha",
                    "confidence": c.get("confidence", 0),
                })
                if len(contacts) >= limit:
                    break
            return contacts
        except Exception as e:
            logger.warning("Lusha contacts search error: %s", e)
            return []


async def enrich_company(domain: str) -> dict | None:
    """Enrich a company by domain via Lusha Company API.

    Returns: {name, domain, industry, employee_count, hq_city, hq_state,
              hq_country, website, linkedin_url, source: "lusha"} or None.
    """
    api_key = settings.lusha_api_key
    if not api_key or not domain:
        return None

    async with _semaphore:
        try:
            resp = await http.get(
                f"{LUSHA_BASE}/company",
                params={"domain": domain},
                headers={"api_key": api_key},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning("Lusha company enrich failed: %s %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            location = data.get("location") or {}
            return {
                "name": data.get("name"),
                "domain": data.get("domain") or domain,
                "industry": data.get("industry"),
                "employee_count": data.get("employeeCount"),
                "hq_city": location.get("city"),
                "hq_state": location.get("state"),
                "hq_country": location.get("country"),
                "website": data.get("website"),
                "linkedin_url": data.get("linkedinUrl"),
                "source": "lusha",
            }
        except Exception as e:
            logger.warning("Lusha company enrich error: %s", e)
            return None
