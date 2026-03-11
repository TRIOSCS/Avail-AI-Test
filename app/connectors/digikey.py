"""DigiKey Product Search API connector.

Handles OAuth2 client_credentials token with expiry tracking, 429 rate
limiting with Retry-After, and 401 token refresh.

Called by: search_service via BaseConnector.search()
Depends on: http_client, utils, sources.BaseConnector
"""

import asyncio
import time

from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from .sources import BaseConnector, _parse_retry_after


class DigiKeyConnector(BaseConnector):
    """DigiKey Product Search v4 — OAuth2 client credentials."""

    # DigiKey uses a two-legged OAuth for product data (client_credentials)
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(self, client_id: str, client_secret: str):
        super().__init__(timeout=15.0)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0  # monotonic time when token expires

    async def _get_token(self) -> str:
        # Return cached token if still valid (with 60s safety margin)
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        self._token = None

        r = await http.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        # DigiKey tokens typically expire in 599s; track it
        expires_in = int(body.get("expires_in", 590))
        self._token_expires_at = time.monotonic() + expires_in
        logger.debug(f"DigiKey: new token acquired, expires in {expires_in}s")
        return self._token

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "Content-Type": "application/json",
            "X-DIGIKEY-Locale-Site": "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "USD",
        }

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.client_id:
            return []
        token = await self._get_token()

        payload = {
            "Keywords": part_number,
            "RecordCount": 25,
            "RecordStartPosition": 0,
            "ExcludeMarketPlaceProducts": False,
        }

        r = await http.post(
            self.SEARCH_URL,
            headers=self._headers(token),
            json=payload,
            timeout=self.timeout,
        )

        # 401 — token expired, refresh once and retry
        if r.status_code == 401:
            self._token = None
            token = await self._get_token()
            r = await http.post(
                self.SEARCH_URL,
                headers=self._headers(token),
                json=payload,
                timeout=self.timeout,
            )

        # 429 — rate limited; wait and retry once inside _do_search,
        # BaseConnector.search() handles additional retries
        if r.status_code == 429:
            retry_after = _parse_retry_after(r)
            logger.warning(f"DigiKey: 429 rate limited for {part_number}, waiting {retry_after:.1f}s")
            await asyncio.sleep(retry_after)
            r = await http.post(
                self.SEARCH_URL,
                headers=self._headers(token),
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code == 429:
                logger.warning(f"DigiKey: still rate limited for {part_number}, returning empty")
                return []

        r.raise_for_status()
        data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        products = data.get("Products") or data.get("products") or []
        results = []

        for prod in products:
            mpn = prod.get("ManufacturerPartNumber") or prod.get("manufacturerPartNumber") or pn
            mfr = (prod.get("Manufacturer") or prod.get("manufacturer") or {}).get("Name", "")
            dk_pn = prod.get("DigiKeyPartNumber") or prod.get("digiKeyPartNumber") or ""
            qty = prod.get("QuantityAvailable") or prod.get("quantityAvailable")
            desc = prod.get("Description") or prod.get("description") or {}
            detail_desc = desc.get("DetailedDescription", "") if isinstance(desc, dict) else str(desc)
            url = prod.get("ProductUrl") or prod.get("productUrl") or ""

            # Price — use unit price or first price break
            price = None
            price_breaks = prod.get("StandardPricing") or prod.get("standardPricing") or []
            if price_breaks and isinstance(price_breaks, list):
                # Find the smallest qty price break
                best = min(
                    price_breaks,
                    key=lambda p: p.get("BreakQuantity", p.get("breakQuantity", 999999)),
                )
                price = best.get("UnitPrice", best.get("unitPrice"))
            else:
                price = prod.get("UnitPrice") or prod.get("unitPrice")

            results.append(
                {
                    "vendor_name": "DigiKey",
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": safe_int(qty),
                    "unit_price": safe_float(price),
                    "currency": "USD",
                    "source_type": "digikey",
                    "is_authorized": True,
                    "confidence": 5 if qty else 3,
                    "click_url": url if url.startswith("http") else f"https://www.digikey.com{url}" if url else "",
                    "vendor_sku": dk_pn,
                    "vendor_url": "https://www.digikey.com",
                    "description": detail_desc,
                }
            )

        logger.info(f"DigiKey: {pn} -> {len(results)} results")
        return results
