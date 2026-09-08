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
Depends on: app.connectors.ebay (token helpers), app.connectors.errors,
            app.connectors.sources (_parse_retry_after), app.http_client
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ...connectors.ebay import EBAY_SEARCH_URL, get_ebay_access_token, invalidate_ebay_token
from ...connectors.errors import ConnectorRateLimitError
from ...connectors.sources import _parse_retry_after
from ...http_client import http

# Only these buying options produce a quotable price. Auctions are excluded
# unless the worker is explicitly configured to include them.
FIXED_PRICE_FILTER = "buyingOptions:{FIXED_PRICE|BEST_OFFER}"


@dataclass
class CallCounter:
    """Browse API calls actually spent by one ``search_mpn`` invocation.

    The worker books its daily budget from this object rather than from
    ``search_mpn``'s return value, because a search that raises (a 5xx on page
    2, an outer timeout that cancels the coroutine) has still spent the calls
    it already made. Returning the count only on success under-reported real
    spend and let the worker run past EBAY_DAILY_CALL_BUDGET.
    """

    calls: int = 0


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


async def _get_page(
    mpn: str,
    config,
    client_id: str,
    client_secret: str,
    offset: int,
    counter: CallCounter,
) -> dict[str, Any]:
    """Fetch one page, re-minting the bearer once on a 401 and honoring a 429.

    Every HTTP request to the Browse API increments ``counter`` BEFORE the
    response is inspected — a retried request spends budget whether or not it
    succeeds.
    """
    token = await get_ebay_access_token(client_id, client_secret)
    params = build_params(mpn, config, offset)
    headers = _headers(token, config.EBAY_MARKETPLACE_ID)
    counter.calls += 1
    r = await http.get(
        EBAY_SEARCH_URL,
        headers=headers,
        params=params,
        timeout=config.EBAY_SEARCH_TIMEOUT_SECONDS,
    )
    if r.status_code == 401:
        # Expired/revoked bearer — drop the cached one and retry once.
        invalidate_ebay_token(client_id)
        token = await get_ebay_access_token(client_id, client_secret)
        headers = _headers(token, config.EBAY_MARKETPLACE_ID)
        counter.calls += 1
        r = await http.get(
            EBAY_SEARCH_URL,
            headers=headers,
            params=params,
            timeout=config.EBAY_SEARCH_TIMEOUT_SECONDS,
        )
    # 429 — the app-level call rate is exceeded. Mirrors EbayConnector._do_search
    # (the same endpoint): honor Retry-After with one inline retry, then surface a
    # TYPED error so the worker re-queues the item and backs off instead of failing
    # it permanently. Without this a throttle burst silently stripped eBay coverage
    # from one requirement per 429.
    if r.status_code == 429:
        retry_after = _parse_retry_after(r)
        logger.warning("EBAY search: 429 rate limited for {}, waiting {:.1f}s", mpn, retry_after)
        await asyncio.sleep(retry_after)
        counter.calls += 1
        r = await http.get(
            EBAY_SEARCH_URL,
            headers=headers,
            params=params,
            timeout=config.EBAY_SEARCH_TIMEOUT_SECONDS,
        )
        if r.status_code == 429:
            raise ConnectorRateLimitError(f"eBay rate limited (persistent 429): {r.text[:200]}")
    # NOTE: 404 is deliberately NOT swallowed into an empty result set. This endpoint
    # answers 200 with no itemSummaries when eBay simply has nothing; a 404 means the
    # endpoint/marketplace is wrong, and fabricating "0 results" would mark the search
    # COMPLETED and suppress re-search of that MPN for the whole dedup window.
    r.raise_for_status()
    payload: dict[str, Any] = r.json()
    return payload


async def search_mpn(
    mpn: str,
    config,
    client_id: str,
    client_secret: str,
    counter: CallCounter | None = None,
) -> tuple[dict[str, Any], int]:
    """Search ``mpn`` across up to EBAY_MAX_PAGES pages.

    Returns ``(merged_payload, calls_made)``. The merged payload keeps the
    Browse API's own shape (``{"itemSummaries": [...], "total": N}``) so the
    parser and the payload hash both work on one object. Paging stops early
    when a page returns fewer items than the page limit — there is nothing
    after a short page.

    Pass ``counter`` to observe the calls spent even when this raises; the
    returned count is always ``counter.calls``.
    """
    merged: list[dict] = []
    counter = counter if counter is not None else CallCounter()
    total: int | None = None
    for page in range(max(1, config.EBAY_MAX_PAGES)):
        offset = page * config.EBAY_PAGE_LIMIT
        payload = await _get_page(mpn, config, client_id, client_secret, offset, counter)
        items = payload.get("itemSummaries") or []
        if total is None:
            total = payload.get("total")
        merged.extend(items)
        if len(items) < config.EBAY_PAGE_LIMIT:
            break
    logger.debug("EBAY search: {} -> {} raw items in {} call(s)", mpn, len(merged), counter.calls)
    return {"itemSummaries": merged, "total": total if total is not None else len(merged)}, counter.calls
