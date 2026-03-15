"""Mouser Search API connector.

Handles 403 Forbidden as rate limiting (Mouser returns 403 when daily
API quota is approached). Returns empty results instead of raising.

Called by: search_service via BaseConnector.search()
Depends on: http_client, utils, sources.BaseConnector
"""

from loguru import logger

from ..http_client import http
from ..utils import safe_float
from .sources import BaseConnector


class MouserConnector(BaseConnector):
    """Mouser Search API — simple API key auth."""

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

        r = await http.post(
            self.SEARCH_URL,
            params={"apiKey": self.api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )

        # 403 — Mouser uses this for rate limiting / quota exceeded
        if r.status_code == 403:
            logger.warning(
                f"Mouser: 403 Forbidden for {part_number} — rate limited or quota near limit, returning empty results"
            )
            return []

        # 429 — explicit rate limit (handled by BaseConnector too, but
        # return empty here to avoid raising)
        if r.status_code == 429:
            logger.warning(f"Mouser: 429 rate limited for {part_number}, returning empty results")
            return []

        r.raise_for_status()
        data = r.json()

        # Mouser returns errors in body even on HTTP 200
        errors = data.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "Unknown Mouser API error")
            # Quota/rate errors in body — return empty instead of raising
            if "too many" in msg.lower() or "rate" in msg.lower() or "quota" in msg.lower():
                logger.warning(f"Mouser: rate/quota error for {part_number}: {msg}")
                return []
            logger.warning(f"Mouser API errors for {part_number}: {errors}")
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
                import re

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
