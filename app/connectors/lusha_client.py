"""Lusha client — direct dial phone number, email, and company discovery.

API docs: https://www.lusha.com/docs/api/v2/
Called by: enrichment_service.find_suggested_contacts(), customer_enrichment_service waterfall.
Depends on: app.config.settings.lusha_api_key, app.http_client.http
"""

import asyncio
from loguru import logger

from app.config import settings
from app.http_client import http


LUSHA_BASE = "https://api.lusha.com"
_semaphore = asyncio.Semaphore(10)

# Confidence map: Lusha email confidence grades → numeric score
_CONFIDENCE_MAP = {"A+": 95, "A": 90, "B": 75, "C": 50}


def _best_phone(phones: list[dict]) -> tuple[str | None, str | None]:
    """Pick the best phone from Lusha's phone list.

    Priority: direct_dial/direct > mobile > phone/work > other.
    Returns (number, type) or (None, None).
    """
    if not phones:
        return None, None
    priority = {"direct_dial": 0, "direct": 0, "mobile": 1, "phone": 2, "work": 2}
    ranked = sorted(phones, key=lambda p: priority.get(p.get("type") or p.get("phoneType", ""), 99))
    best = ranked[0]
    return best.get("number"), best.get("type") or best.get("phoneType")


def _best_email(emails: list[dict]) -> tuple[str | None, int]:
    """Pick the best email from Lusha's email list.

    Priority: work > personal > other.
    Returns (email, confidence_score) or (None, 0).
    """
    if not emails:
        return None, 0
    priority = {"work": 0, "personal": 1}
    ranked = sorted(emails, key=lambda e: priority.get(e.get("type") or e.get("emailType", ""), 99))
    best = ranked[0]
    conf = _CONFIDENCE_MAP.get(best.get("emailConfidence", ""), 50)
    return best.get("email"), conf


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
                f"{LUSHA_BASE}/v2/person",
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
            best_email, confidence = _best_email(emails)

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
                "confidence": confidence,
            }
        except Exception as e:
            logger.warning("Lusha person lookup error: %s", e)
            return None


async def search_contacts(
    company_domain: str,
    titles: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search for contacts at a company via Lusha Prospecting API (2-step).

    Step 1: POST /prospecting/contact/search — find contacts by domain + filters.
    Step 2: POST /prospecting/contact/enrich — enrich top results for emails/phones.

    Filters by title keywords locally after enrichment.
    Returns list of contact dicts with full_name, title, email, phone, etc.
    """
    api_key = settings.lusha_api_key
    if not api_key or not company_domain:
        return []

    title_set = {t.lower() for t in (titles or [])}

    async with _semaphore:
        try:
            # Step 1: Search
            search_resp = await http.post(
                f"{LUSHA_BASE}/prospecting/contact/search",
                headers={"api_key": api_key, "Content-Type": "application/json"},
                json={
                    "filters": {
                        "companies": {
                            "include": {"domains": [company_domain]},
                        },
                    },
                },
                timeout=25,
            )
            if search_resp.status_code not in (200, 201):
                logger.warning("Lusha prospecting search failed: %s %s", search_resp.status_code, search_resp.text[:200])
                return []

            search_data = search_resp.json()
            request_id = search_data.get("requestId")
            candidates = search_data.get("data") or []
            if not candidates or not request_id:
                return []

            # Pre-filter by title before enriching to save credits
            if title_set:
                filtered = [
                    c for c in candidates
                    if any(kw in (c.get("jobTitle") or "").lower() for kw in title_set)
                ]
                candidates = filtered or candidates[:limit]

            # Take top N for enrichment
            to_enrich = candidates[:limit]
            contact_ids = [c["contactId"] for c in to_enrich]

            # Step 2: Enrich
            enrich_resp = await http.post(
                f"{LUSHA_BASE}/prospecting/contact/enrich",
                headers={"api_key": api_key, "Content-Type": "application/json"},
                json={"requestId": request_id, "contactIds": contact_ids},
                timeout=25,
            )
            if enrich_resp.status_code not in (200, 201):
                logger.warning("Lusha prospecting enrich failed: %s %s", enrich_resp.status_code, enrich_resp.text[:200])
                return []

            enriched = enrich_resp.json().get("contacts") or []
            contacts = []
            for item in enriched:
                if not item.get("isSuccess"):
                    continue
                c = item.get("data") or {}
                phones = c.get("phoneNumbers") or []
                phone, phone_type = _best_phone(phones)
                emails = c.get("emailAddresses") or []
                best_email, confidence = _best_email(emails)
                linkedin = (c.get("socialLinks") or {}).get("linkedin")
                contacts.append({
                    "full_name": c.get("fullName"),
                    "title": c.get("jobTitle"),
                    "email": best_email,
                    "phone": phone,
                    "phone_type": phone_type,
                    "linkedin_url": linkedin,
                    "source": "lusha",
                    "confidence": confidence,
                })
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
                f"{LUSHA_BASE}/v2/company",
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
