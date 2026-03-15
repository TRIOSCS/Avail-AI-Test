"""Element14 / Newark Search API connector.

Searches the element14 product catalog (Newark US store) by manufacturer
part number. REST API with simple API key auth. Falls back to keyword
search if exact MPN match returns no results.

Called by: search_service.py
Depends on: BaseConnector, httpx
"""

from urllib.parse import quote_plus

from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from .sources import BaseConnector


class Element14Connector(BaseConnector):
    """Element14 Product Search — API key auth, Newark US store."""

    SEARCH_URL = "https://api.element14.com/catalog/products"

    def __init__(self, api_key: str):
        super().__init__(timeout=15.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        # Try exact MPN search first
        results = await self._api_search(f"manuPartNum:{part_number}", part_number)
        if results:
            return results

        # Fallback: keyword search (catches partial matches and alternate formats)
        logger.debug(f"element14: exact MPN match returned 0 for {part_number}, trying keyword search")
        return await self._api_search(part_number, part_number)

    async def _api_search(self, term: str, part_number: str) -> list[dict]:
        """Run a single search against the element14 API."""
        params = {
            "term": term,
            "storeInfo.id": "www.newark.com",
            "resultsSettings.offset": "0",
            "resultsSettings.numberOfResults": "25",
            "resultsSettings.responseGroup": "large",
            "callInfo.apiKey": self.api_key,
            "callInfo.responseDataFormat": "json",
        }

        r = await http.get(self.SEARCH_URL, params=params, timeout=self.timeout)
        if r.status_code == 400:
            logger.debug(f"element14: 400 Bad Request for term '{term}' — skipping")
            return []
        r.raise_for_status()
        data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        container = data.get("manufacturerPartNumberSearchReturn", {})
        products = container.get("products", [])
        results = []

        for prod in products:
            mpn = prod.get("translatedManufacturerPartNumber") or pn
            mfr = prod.get("brandName", "")
            desc = prod.get("displayName", "")
            sku = prod.get("sku", "")

            # Stock
            stock_info = prod.get("stock", {})
            qty = safe_int(stock_info.get("level")) if stock_info else None

            # Price — first price break
            price = None
            prices = prod.get("prices", [])
            if prices:
                price = safe_float(prices[0].get("cost"))

            click_url = f"https://www.newark.com/search?st={quote_plus(mpn)}"

            results.append(
                {
                    "vendor_name": "element14",
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": qty,
                    "unit_price": round(price, 4) if price else None,
                    "currency": "USD",
                    "source_type": "element14",
                    "is_authorized": True,
                    "confidence": 5 if qty and qty > 0 else 3,
                    "click_url": click_url,
                    "vendor_sku": sku,
                    "vendor_url": "https://www.newark.com",
                    "description": desc[:500] if desc else "",
                }
            )

        logger.info(f"element14: {pn} -> {len(results)} results")
        return results
