"""Hunter.io enrichment connector — domain email discovery.

Searches Hunter.io for email addresses and contacts at a given company domain.
Used in the vendor/company enrichment pipeline to populate contact emails for RFQ outreach.

Called by: app/enrichment_service.py (find_suggested_contacts waterfall)
Depends on: app/http_client.py, HUNTER_API_KEY credential
"""

from loguru import logger

from ..http_client import http
from ..services.enrichment_credit_guard import ProviderQuotaError
from .errors import ConnectorAuthError

_BASE = "https://api.hunter.io/v2"
_QUOTA_STATUSES = (402, 429)


class HunterConnector:
    """Hunter.io API client for domain email discovery."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def domain_search(self, domain: str, limit: int = 10) -> list[dict]:
        """Return contacts found at *domain* via Hunter.io domain-search.

        Each contact dict has: email, confidence, type, first_name, last_name,
        position, linkedin_url, phone_number.
        """
        if not self.api_key or not domain:
            return []

        try:
            r = await http.get(
                f"{_BASE}/domain-search",
                params={
                    "domain": domain,
                    "limit": limit,
                    "api_key": self.api_key,
                },
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning("Hunter domain-search network error for {}: {}", domain, exc)
            return []

        if r.status_code == 401:
            raise ConnectorAuthError("Hunter.io auth error: HTTP 401 — check HUNTER_API_KEY")
        if r.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Hunter.io domain-search quota/rate-limit: {r.status_code}")
        if r.status_code != 200:
            logger.warning("Hunter domain-search HTTP {} for {}", r.status_code, domain)
            return []

        data = r.json()
        emails = (data.get("data") or {}).get("emails") or []
        results = []
        for e in emails:
            value = e.get("value") or ""
            if not value or "@" not in value:
                continue
            results.append(
                {
                    "email": value,
                    "confidence": e.get("confidence", 0),
                    "type": e.get("type", ""),
                    "first_name": e.get("first_name") or "",
                    "last_name": e.get("last_name") or "",
                    "position": e.get("position") or "",
                    "linkedin_url": e.get("linkedin") or "",
                    "phone_number": e.get("phone_number") or "",
                }
            )

        logger.debug("Hunter domain-search {}: {} contacts", domain, len(results))
        return results

    async def email_finder(self, domain: str, first_name: str, last_name: str) -> dict | None:
        """Find a specific person's email at *domain*.

        Returns {email, score} or None.
        """
        if not self.api_key or not domain or not first_name or not last_name:
            return None

        try:
            r = await http.get(
                f"{_BASE}/email-finder",
                params={
                    "domain": domain,
                    "first_name": first_name,
                    "last_name": last_name,
                    "api_key": self.api_key,
                },
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning("Hunter email-finder error: {}", exc)
            return None

        if r.status_code == 401:
            raise ConnectorAuthError("Hunter.io auth error: HTTP 401")
        if r.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Hunter.io email-finder quota/rate-limit: {r.status_code}")
        if r.status_code != 200:
            return None

        data = r.json()
        email = (data.get("data") or {}).get("email") or ""
        score = (data.get("data") or {}).get("score") or 0
        return {"email": email, "score": score} if email else None

    async def verify(self, email: str) -> dict:
        """Verify an email address.

        Returns {result, score} where result ∈ {deliverable, risky, undeliverable,
        unknown}.
        """
        if not self.api_key or not email:
            return {"result": "unknown", "score": 0}

        try:
            r = await http.get(
                f"{_BASE}/email-verifier",
                params={"email": email, "api_key": self.api_key},
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning("Hunter verify error: {}", exc)
            return {"result": "unknown", "score": 0}

        if r.status_code == 401:
            raise ConnectorAuthError("Hunter.io auth error: HTTP 401")
        if r.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Hunter verify quota/rate-limit: {r.status_code}")
        if r.status_code != 200:
            return {"result": "unknown", "score": 0}

        data = r.json()
        d = data.get("data") or {}
        return {"result": d.get("result", "unknown"), "score": d.get("score", 0)}
