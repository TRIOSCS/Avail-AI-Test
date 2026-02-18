"""TME (Transfer Multisort Elektronik) API connector.

Searches TME product catalog using HMAC-SHA256 signed requests.
Two-step: search for products, then fetch prices.

Called by: search_service.py
Depends on: BaseConnector, httpx, hmac
"""

import base64
import hashlib
import hmac
import logging
import urllib.parse

from .sources import BaseConnector
from ..http_client import http
from ..utils import safe_int, safe_float

log = logging.getLogger(__name__)


class TMEConnector(BaseConnector):
    """TME Product Search — HMAC-SHA256 signed POST requests."""

    SEARCH_URL = "https://api.tme.eu/Products/Search.json"
    PRICES_URL = "https://api.tme.eu/Products/GetPrices.json"

    def __init__(self, token: str, secret: str):
        super().__init__(timeout=15.0)
        self.token = token
        self.secret = secret

    def _sign(self, url: str, params: dict) -> dict:
        """Sign a TME API request with HMAC-SHA256."""
        params = dict(params)
        params["Token"] = self.token
        sorted_params = sorted(params.items())
        encoded = urllib.parse.urlencode(sorted_params)
        base_string = (
            f"POST&{urllib.parse.quote(url, safe='')}"
            f"&{urllib.parse.quote(encoded, safe='')}"
        )
        signature = hmac.new(
            self.secret.encode(),
            base_string.encode(),
            hashlib.sha256,
        ).digest()
        params["ApiSignature"] = base64.b64encode(signature).decode()
        return params

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.token or not self.secret:
            return []

        # Step 1: Search for products
        search_params = {
            "SearchPlain": part_number,
            "Country": "US",
            "Language": "EN",
        }
        signed = self._sign(self.SEARCH_URL, search_params)

        r = await http.post(
            self.SEARCH_URL,
            data=signed,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()

        products = (data.get("Data") or {}).get("ProductList", [])
        if not products:
            log.info(f"TME: {part_number} -> 0 results")
            return []

        # Step 2: Fetch prices for found products
        symbols = [p.get("Symbol", "") for p in products[:25] if p.get("Symbol")]
        prices_map = {}
        if symbols:
            prices_map = await self._fetch_prices(symbols)

        return self._parse(products, prices_map, part_number)

    async def _fetch_prices(self, symbols: list[str]) -> dict:
        """Fetch prices for a list of TME symbols. Returns {symbol: price}."""
        params = {"Country": "US", "Currency": "USD"}
        for i, sym in enumerate(symbols):
            params[f"SymbolList[{i}]"] = sym

        signed = self._sign(self.PRICES_URL, params)

        try:
            r = await http.post(
                self.PRICES_URL,
                data=signed,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(f"TME price fetch failed: {e}")
            return {}

        result = {}
        for item in (data.get("Data") or {}).get("ProductList", []):
            symbol = item.get("Symbol", "")
            price_list = item.get("PriceList", [])
            if price_list:
                # Use first (lowest quantity) price break
                result[symbol] = safe_float(price_list[0].get("PriceValue"))
        return result

    def _parse(
        self, products: list, prices_map: dict, pn: str
    ) -> list[dict]:
        results = []

        for prod in products:
            symbol = prod.get("Symbol", "")
            mpn = prod.get("OriginalSymbol") or symbol or pn
            mfr = prod.get("Producer", "")
            desc = prod.get("Description", "")
            qty = safe_int(prod.get("QuantityAvailable"))
            price = prices_map.get(symbol)

            click_url = f"https://www.tme.eu/us/details/{urllib.parse.quote(symbol)}/"

            results.append({
                "vendor_name": "TME",
                "manufacturer": mfr,
                "mpn_matched": mpn,
                "qty_available": qty,
                "unit_price": round(price, 4) if price else None,
                "currency": "USD",
                "source_type": "tme",
                "is_authorized": True,
                "confidence": 5 if qty and qty > 0 else 3,
                "click_url": click_url,
                "vendor_sku": symbol,
                "vendor_url": "https://www.tme.eu",
                "description": desc[:500] if desc else "",
            })

        log.info(f"TME: {pn} -> {len(results)} results")
        return results
