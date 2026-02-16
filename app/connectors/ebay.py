"""eBay Browse API connector — searches electronic components on eBay."""

import logging
import base64
import httpx
from .sources import BaseConnector

log = logging.getLogger(__name__)

# eBay category IDs for electronic components
EBAY_CATEGORIES = {
    "semiconductors": "180021",
    "active_components": "58058",
    "passive_components": "163843",
    "electronic_components": "175673",
}


class EbayConnector(BaseConnector):
    """eBay Browse API — OAuth client credentials flow."""

    TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, client_id: str, client_secret: str):
        super().__init__(timeout=15.0)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                self.TOKEN_URL,
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]
            return self._token

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.client_id:
            return []
        token = await self._get_token()

        # Search in Semiconductors & Actives category (broader electronics)
        params = {
            "q": part_number,
            "category_ids": "175673",  # Electronic Components & Semiconductors
            "limit": "30",
            "fieldgroups": "MATCHING_ITEMS",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(
                self.SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    "Content-Type": "application/json",
                },
                params=params,
            )

            if r.status_code == 401:
                self._token = None
                token = await self._get_token()
                r = await c.get(
                    self.SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                        "Content-Type": "application/json",
                    },
                    params=params,
                )

            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        items = data.get("itemSummaries", [])
        results = []
        seen = set()

        for item in items:
            seller = item.get("seller", {})
            seller_name = seller.get("username", "")
            if not seller_name:
                continue

            title = item.get("title", "")
            price_info = item.get("price", {})
            price = _safe_float(price_info.get("value"))
            currency = price_info.get("currency", "USD")

            condition = item.get("condition", "")
            item_url = item.get("itemWebUrl", "")
            item_id = item.get("itemId", "")
            image = (item.get("image") or {}).get("imageUrl", "")

            # Availability from estimated qty
            qty = None
            if item.get("estimatedAvailabilities"):
                for avail in item["estimatedAvailabilities"]:
                    est = avail.get("estimatedAvailableQuantity")
                    if est:
                        qty = _safe_int(est)
                        break

            key = f"{seller_name}_{item_id}"
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "vendor_name": seller_name,
                    "manufacturer": "",  # eBay doesn't reliably provide this
                    "mpn_matched": pn,
                    "qty_available": qty,
                    "unit_price": price,
                    "currency": currency,
                    "source_type": "ebay",
                    "is_authorized": False,
                    "confidence": 3 if qty else 2,
                    "click_url": item_url,
                    "ebay_item_id": item_id,
                    "ebay_title": title,
                    "ebay_condition": condition,
                    "ebay_image": image,
                    "vendor_url": f"https://www.ebay.com/usr/{seller_name}",
                }
            )

        log.info(f"eBay: {pn} -> {len(results)} results")
        return results


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
