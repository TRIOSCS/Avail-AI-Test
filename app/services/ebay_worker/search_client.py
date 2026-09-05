"""EBay Browse API search client.

Thin async wrapper around GET
https://api.ebay.com/buy/browse/v1/item_summary/search — the ONLY eBay
endpoint this worker touches. Item detail pages are never fetched: everything
the sighting needs (seller, price, condition, availability, deep link) is in
the search response, and per-item detail calls would burn the daily call
budget an order of magnitude faster.

The OAuth bearer comes from app/connectors/ebay.get_ebay_access_token, which
shares one process-wide cached token with EbayConnector.

Called by: worker loop
Depends on: app.connectors.ebay (token helpers), app.http_client
"""

from typing import Any

from loguru import logger

from ...connectors.ebay import EBAY_SEARCH_URL, get_ebay_access_token, invalidate_ebay_token
from ...http_client import http

# Only these buying options produce a quotable price. Auctions are excluded
# unless the worker is explicitly configured to include them.
FIXED_PRICE_FILTER = "buyingOptions:{FIXED_PRICE|BEST_OFFER}"


def build_params(mpn: str, config, offset: int) -> dict[str, str]:
    """Build one page's Browse API query string.

    ``category_ids`` is only sent when EBAY_CATEGORY_IDS is non-empty — the
    default searches all of eBay, because brokered board-level parts are
    scattered across categories and a filter silently hides them.
    """
    params: dict[str, str] = {
        "q": mpn,
        "limit": str(config.EBAY_PAGE_LIMIT),
        "offset": str(offset),
    }
    if not config.EBAY_INCLUDE_AUCTIONS:
        params["filter"] = FIXED_PRICE_FILTER
    categories = config.category_id_list
    if categories:
        params["category_ids"] = ",".join(categories)
    return params


def _headers(bearer: str, marketplace_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
        "Content-Type": "application/json",
    }


async def _get_page(mpn: str, config, client_id: str, client_secret: str, offset: int) -> dict[str, Any]:
    """Fetch one page, re-minting the bearer once on a 401."""
    token = await get_ebay_access_token(client_id, client_secret)
    params = build_params(mpn, config, offset)
    r = await http.get(
        EBAY_SEARCH_URL,
        headers=_headers(token, config.EBAY_MARKETPLACE_ID),
        params=params,
        timeout=config.EBAY_SEARCH_TIMEOUT_SECONDS,
    )
    if r.status_code == 401:
        # Expired/revoked bearer — drop the cached one and retry once.
        invalidate_ebay_token(client_id)
        token = await get_ebay_access_token(client_id, client_secret)
        r = await http.get(
            EBAY_SEARCH_URL,
            headers=_headers(token, config.EBAY_MARKETPLACE_ID),
            params=params,
            timeout=config.EBAY_SEARCH_TIMEOUT_SECONDS,
        )
    if r.status_code == 404:
        return {"itemSummaries": []}
    r.raise_for_status()
    payload: dict[str, Any] = r.json()
    return payload


async def search_mpn(mpn: str, config, client_id: str, client_secret: str) -> tuple[dict[str, Any], int]:
    """Search ``mpn`` across up to EBAY_MAX_PAGES pages.

    Returns ``(merged_payload, calls_made)``. The merged payload keeps the
    Browse API's own shape (``{"itemSummaries": [...], "total": N}``) so the
    parser and the payload hash both work on one object. Paging stops early
    when a page returns fewer items than the page limit — there is nothing
    after a short page.
    """
    merged: list[dict] = []
    calls = 0
    total: int | None = None
    for page in range(max(1, config.EBAY_MAX_PAGES)):
        offset = page * config.EBAY_PAGE_LIMIT
        payload = await _get_page(mpn, config, client_id, client_secret, offset)
        calls += 1
        items = payload.get("itemSummaries") or []
        if total is None:
            total = payload.get("total")
        merged.extend(items)
        if len(items) < config.EBAY_PAGE_LIMIT:
            break
    logger.debug("EBAY search: {} -> {} raw items in {} call(s)", mpn, len(merged), calls)
    return {"itemSummaries": merged, "total": total if total is not None else len(merged)}, calls
