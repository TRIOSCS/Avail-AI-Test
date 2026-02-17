"""Hunter.io client — email verification and domain contact discovery.

API docs: https://hunter.io/api-documentation
Gracefully returns empty results when API key is not configured.
"""

import asyncio
import logging

import httpx

from app.config import settings

log = logging.getLogger("avail.hunter")

HUNTER_BASE = "https://api.hunter.io/v2"
_semaphore = asyncio.Semaphore(5)


async def verify_email(email: str) -> dict | None:
    """Verify email deliverability via Hunter.io.

    Returns: {
        email, status (valid/invalid/accept_all/webmail/disposable/unknown),
        score (0-100), sources (int)
    } or None.
    """
    api_key = settings.hunter_api_key
    if not api_key or not email:
        return None

    async with _semaphore:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{HUNTER_BASE}/email-verifier",
                    params={"email": email, "api_key": api_key},
                )
                if resp.status_code != 200:
                    log.warning("Hunter verify failed: %s %s", resp.status_code, resp.text[:200])
                    return None

                data = resp.json().get("data", {})
                return {
                    "email": data.get("email", email),
                    "status": data.get("status", "unknown"),
                    "score": data.get("score", 0),
                    "sources": data.get("sources", 0),
                }
        except Exception as e:
            log.warning("Hunter verify error: %s", e)
            return None


async def find_domain_emails(domain: str, limit: int = 10) -> list[dict]:
    """Discover contacts at a domain via Hunter.io.

    Returns: [{
        email, first_name, last_name, full_name, position, department,
        linkedin_url, phone_number, confidence, source: "hunter"
    }]
    """
    api_key = settings.hunter_api_key
    if not api_key or not domain:
        return []

    async with _semaphore:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{HUNTER_BASE}/domain-search",
                    params={
                        "domain": domain,
                        "api_key": api_key,
                        "limit": min(limit, 100),
                        "type": "personal",
                    },
                )
                if resp.status_code != 200:
                    log.warning("Hunter domain search failed: %s %s", resp.status_code, resp.text[:200])
                    return []

                data = resp.json().get("data", {})
                emails = data.get("emails", [])

                contacts = []
                for e in emails[:limit]:
                    first = (e.get("first_name") or "").strip()
                    last = (e.get("last_name") or "").strip()
                    full_name = f"{first} {last}".strip() if first or last else None

                    contacts.append({
                        "email": e.get("value"),
                        "first_name": first or None,
                        "last_name": last or None,
                        "full_name": full_name,
                        "position": e.get("position"),
                        "department": e.get("department"),
                        "linkedin_url": e.get("linkedin"),
                        "phone_number": e.get("phone_number"),
                        "confidence": e.get("confidence", 0),
                        "source": "hunter",
                    })

                return contacts
        except Exception as e:
            log.warning("Hunter domain search error: %s", e)
            return []
