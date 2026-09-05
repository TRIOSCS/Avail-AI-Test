"""EBay Browse API connector — searches electronic components on eBay.

Also the home of the shared OAuth client-credentials token helpers
(``ebay_token_cache_key`` / ``get_ebay_access_token`` /
``invalidate_ebay_token``). The ebay_worker API poller reuses them so there is
ONE eBay bearer mint + one process-wide cache entry, rather than a duplicate
implementation on the worker side. They live here (not on the connector class)
so a caller that does not want this connector's category-restricted,
30-result search can still get a token.
"""

import asyncio
import base64

from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from .errors import ConnectorRateLimitError
from .sources import BaseConnector, _get_cached_token, _invalidate_token, _parse_retry_after

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def ebay_token_cache_key(client_id: str) -> tuple[str, str]:
    """Process-wide OAuth cache key for an eBay client id.

    Deliberately identical to ``BaseConnector._token_cache_key()`` for
    EbayConnector so the connector and the worker share ONE cached bearer.
    """
    return ("EbayConnector", client_id)


async def get_ebay_access_token(client_id: str, client_secret: str) -> str:
    """Return a cached eBay application bearer, minting one only when needed.

    Client-credentials grant against the api_scope. The bearer is cached
    process-wide by ``_get_cached_token`` (per-key lock, expiry-aware), so
    concurrent callers collapse into a single token POST.
    """

    async def _mint() -> tuple[str, int]:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = await http.post(
            EBAY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        expires_in = int(body.get("expires_in", 7200))
        logger.debug("eBay: new token acquired, expires in {}s", expires_in)
        return body["access_token"], expires_in

    return await _get_cached_token(ebay_token_cache_key(client_id), _mint)


def invalidate_ebay_token(client_id: str) -> None:
    """Drop the cached bearer for ``client_id`` so the next call re-mints (after a
    401)."""
    _invalidate_token(ebay_token_cache_key(client_id))


class EbayConnector(BaseConnector):
    """EBay Browse API — OAuth client credentials flow.

    Retained for the Settings -> Connectors Test button, the health_monitor
    ping, and enrichment's eBay title mining. Requirement fan-out no longer
    goes through it: eBay search is worker-backed (app/services/ebay_worker).
    """

    source_name: str = "ebay"

    TOKEN_URL = EBAY_TOKEN_URL
    SEARCH_URL = EBAY_SEARCH_URL

    def __init__(self, client_id: str, client_secret: str):
        super().__init__(timeout=15.0)
        self.client_id = client_id
        self.client_secret = client_secret

    async def _get_token(self) -> str:
        return await get_ebay_access_token(self.client_id, self.client_secret)

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

        def headers(bearer: str) -> dict:
            return {
                "Authorization": f"Bearer {bearer}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Content-Type": "application/json",
            }

        r = await http.get(self.SEARCH_URL, headers=headers(token), params=params, timeout=self.timeout)

        if r.status_code == 401:
            invalidate_ebay_token(self.client_id)
            token = await self._get_token()
            r = await http.get(self.SEARCH_URL, headers=headers(token), params=params, timeout=self.timeout)

        # 429 — rate limited; honor Retry-After (capped at 30s by _parse_retry_after,
        # the Phase-1 cap) with one inline retry, then surface a typed error. Mirrors
        # DigiKeyConnector — eBay's OAuth-client-credentials sibling — so a 429 is handled
        # EXPLICITLY here (typed ConnectorRateLimitError) rather than lumped into the
        # generic raise_for_status path.
        if r.status_code == 429:
            retry_after = _parse_retry_after(r)
            logger.warning(f"eBay: 429 rate limited for {part_number}, waiting {retry_after:.1f}s")
            await asyncio.sleep(retry_after)
            r = await http.get(self.SEARCH_URL, headers=headers(token), params=params, timeout=self.timeout)
            if r.status_code == 429:
                raise ConnectorRateLimitError(f"eBay rate limited (persistent 429): {r.text[:200]}")

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
            price = safe_float(price_info.get("value"))
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
                        qty = safe_int(est)
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

        logger.info(f"eBay: {pn} -> {len(results)} results")
        return results
