"""OEMSecrets API connector — meta-aggregator across 140+ distributors.

Returns pricing/stock from DigiKey, Mouser, Arrow, Avnet, Farnell, RS,
Future, TME, and many more in a single API call.

Called by: app/connectors/sources.py via search pipeline
Depends on: http_client, utils.safe_float/safe_int, sources.BaseConnector
"""

import httpx
from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from .sources import BaseConnector


class OEMSecretsConnector(BaseConnector):
    """OEMSecrets Part Search API — JSON endpoint."""

    # Docs: https://oemsecretsapi.com/documentation/
    SEARCH_URL = "https://oemsecretsapi.com/partsearch"

    def __init__(self, api_key: str):
        super().__init__(timeout=20.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        params = {
            "apiKey": self.api_key,
            "searchTerm": part_number,
            "currency": "USD",
        }

        r = await http.get(self.SEARCH_URL, params=params, timeout=httpx.Timeout(self.timeout, connect=5.0))

        # 401 — OEMSecrets returns this when API key is rejected OR quota
        # is exhausted ("User is not accepted or has run out of api calls").
        # Return empty instead of raising to avoid tripping the circuit breaker
        # on a temporary quota issue.
        if r.status_code == 401:
            body_preview = r.text[:200]
            logger.warning(f"OEMSecrets: 401 for {part_number} — quota exhausted or key invalid: {body_preview}")
            return []

        # 429 — explicit rate limit
        if r.status_code == 429:
            logger.warning(f"OEMSecrets: 429 rate limited for {part_number}, returning empty results")
            return []

        if r.status_code != 200:
            logger.warning(f"OEMSecrets: HTTP {r.status_code} for {part_number}: {r.text[:200]}")
            r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            logger.warning(f"OEMSecrets: non-JSON response for {part_number}: {r.text[:200]}")
            return []

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        """Parse OEMSecrets API response into normalized results.

        API response has top-level 'stock' array. Each item contains:
        - distributor: {distributor_name, ...}
        - quantity_in_stock, source_part_number, part_number
        - prices: {USD: [{unit_break, unit_price}, ...], ...}
        - buy_now_url, datasheet_url, manufacturer, moq
        """
        stock_data = data if isinstance(data, list) else data.get("stock", data.get("results", []))
        if not isinstance(stock_data, list):
            stock_data = []

        results = []
        seen = set()

        for item in stock_data:
            if not isinstance(item, dict):
                continue

            # Distributor name — nested dict or flat string
            distributor = item.get("distributor", {})
            if isinstance(distributor, dict):
                dist_name = distributor.get("distributor_name", distributor.get("name", ""))
            else:
                dist_name = str(distributor)
            if not dist_name:
                dist_name = item.get("distributor_name", item.get("seller", ""))
            if not dist_name:
                continue

            # MPN — prefer source_part_number (has original formatting)
            mpn = item.get("source_part_number", item.get("part_number", item.get("mpn", pn)))
            mfr = item.get("manufacturer", "")

            # Quantity — v3 uses quantity_in_stock
            qty = item.get("quantity_in_stock", item.get("stock", item.get("quantity", item.get("qty"))))

            # Price — extract from nested prices dict or flat field
            price = None
            prices_obj = item.get("prices")
            if isinstance(prices_obj, dict):
                # Pick USD prices, fall back to first available currency
                price_list = prices_obj.get("USD", [])
                if not price_list:
                    for _cur, plist in prices_obj.items():
                        if plist:
                            price_list = plist
                            break
                if price_list and isinstance(price_list, list) and len(price_list) > 0:
                    # Use the lowest unit break price (first entry)
                    price = price_list[0].get("unit_price")
            if price is None:
                price = item.get("price", item.get("unit_price"))

            currency = item.get("currency", "USD")
            url = item.get("buy_now_url", item.get("url", item.get("buy_url", "")))
            moq = item.get("moq", item.get("minimum_order"))
            sku = str(item.get("sku", item.get("distributor_pn", "")) or "")
            datasheet = item.get("datasheet_url", "")

            key = f"{dist_name}_{mpn}_{sku}".lower()
            if key in seen:
                continue
            seen.add(key)

            # Authorization status
            auth_status = item.get("distributor_authorisation_status", "")
            is_auth = (
                auth_status == "authorised" if auth_status else item.get("authorized", item.get("is_authorized", True))
            )

            results.append(
                {
                    "vendor_name": dist_name,
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": safe_int(qty),
                    "unit_price": safe_float(price),
                    "currency": currency,
                    "source_type": "oemsecrets",
                    "is_authorized": bool(is_auth),
                    "confidence": 5 if qty else 3,
                    "click_url": url,
                    "vendor_sku": sku,
                    "moq": safe_int(moq) or None,
                    "datasheet_url": datasheet,
                }
            )

        logger.info(f"OEMSecrets: {pn} -> {len(results)} results")
        return results
