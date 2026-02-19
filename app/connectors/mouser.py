"""Mouser Search API connector."""

import logging
from .sources import BaseConnector
from ..http_client import http
from ..utils import safe_int, safe_float

log = logging.getLogger(__name__)


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
            f"{self.SEARCH_URL}?apiKey={self.api_key}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()

        # Log Mouser API errors (returned in body even on 200)
        errors = data.get("Errors") or []
        if errors:
            log.warning(f"Mouser API errors for {part_number}: {errors}")

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
            part.get("ImagePath", "")

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

        log.info(f"Mouser: {pn} -> {len(results)} results")
        return results
