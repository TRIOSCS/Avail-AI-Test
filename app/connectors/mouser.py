"""Mouser API connector.

Hard errors (HTTP 401/403/429, body-level auth/rate errors) raise typed ConnectorError
subclasses; health_monitor flips api_sources.status to 'error' and search_service
excludes the source from user searches. Auto-recovers when the next ping returns 200.

See docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
"""

import re

from loguru import logger

from ..http_client import http
from ..utils import safe_float
from .errors import ConnectorAuthError, ConnectorRateLimitError
from .sources import BaseConnector


class MouserConnector(BaseConnector):
    """Mouser Search API — simple API key auth."""

    source_name: str = "mouser"

    SEARCH_URL = "https://api.mouser.com/api/v2/search/keyword"

    def __init__(self, api_key: str):
        super().__init__(timeout=15.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        payload = {
            "SearchByKeywordRequest": {
                "keyword": part_number,
                "records": 25,
                "startingRecord": 0,
                "searchOptions": "1",  # InStock
                "searchWithYourSignUpLanguage": "en",
            }
        }

        # Mouser API requires apiKey as URL query param — no header auth option.
        # Sentry before_send hook scrubs query strings containing "key" (see main.py).
        # httpx does not log URL params at INFO level, only at DEBUG/TRACE.
        r = await http.post(
            self.SEARCH_URL,
            params={"apiKey": self.api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )

        # 403 — bad/revoked key, quota-rejected, or region-locked. Raise
        # so health_monitor flips status='error' and the source is excluded
        # from user searches; auto-recovers on next ping success if it was
        # transient.
        if r.status_code == 403:
            raise ConnectorAuthError(f"Mouser auth error: HTTP 403 {r.text[:200]}")

        # 429 — explicit rate limit. Auto-recovers on next ping success.
        if r.status_code == 429:
            raise ConnectorRateLimitError(f"Mouser rate limited: HTTP 429 {r.text[:200]}")

        r.raise_for_status()
        data = r.json()

        # Mouser returns errors in body even on HTTP 200
        errors = data.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "Unknown Mouser API error")
            msg_lower = msg.lower()
            # Quota/rate errors in body — raise rate-limit so status flips
            # to 'error' and the operator sees the chip.
            if "too many" in msg_lower or "rate" in msg_lower or "quota" in msg_lower:
                raise ConnectorRateLimitError(f"Mouser rate/quota error: {msg}")
            # Auth errors (bad / revoked / missing API key)
            is_auth_error = (
                "api key" in msg_lower
                or "unauthorized" in msg_lower
                or ("invalid" in msg_lower and ("identifier" in msg_lower or "key" in msg_lower))
            )
            if is_auth_error:
                raise ConnectorAuthError(f"Mouser auth error: {msg}")
            logger.warning(f"Mouser API errors for {part_number}: {errors}")
            # Catalog errors ("Invalid part number") aren't hard contract
            # failures — keep them as plain RuntimeError so the caller
            # treats them as transient.
            raise RuntimeError(f"Mouser API: {msg}")

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        search_results = data.get("SearchResults") or {}
        parts = search_results.get("Parts") or []
        results = []

        for part in parts:
            mpn = part.get("ManufacturerPartNumber", pn)
            mfr = part.get("Manufacturer", "")
            mouser_pn = part.get("MouserPartNumber", "")
            desc = part.get("Description", "")
            url = part.get("ProductDetailUrl", "")
            avail = part.get("Availability", "")
            # Parse availability string like "In Stock" or "3,500 In Stock"
            qty = None
            if avail:
                match = re.search(r"([\d,]+)\s+In Stock", avail, re.IGNORECASE)
                if match:
                    qty = int(match.group(1).replace(",", ""))
                elif "in stock" in avail.lower():
                    qty = 1  # Unknown qty but in stock

            # Price breaks
            price = None
            price_breaks = part.get("PriceBreaks", [])
            if price_breaks:
                best = min(price_breaks, key=lambda p: p.get("Quantity", 999999))
                price_str = best.get("Price", "")
                if price_str:
                    price = safe_float(price_str.replace("$", "").replace(",", ""))

            results.append(
                {
                    "vendor_name": "Mouser",
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": qty,
                    "unit_price": price,
                    "currency": "USD",
                    "source_type": "mouser",
                    "is_authorized": True,
                    "confidence": 5 if qty and qty > 0 else 3,
                    "click_url": url,
                    "vendor_sku": mouser_pn,
                    "vendor_url": "https://www.mouser.com",
                    "description": desc,
                }
            )

        logger.info(f"Mouser: {pn} -> {len(results)} results")
        return results
