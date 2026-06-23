"""The Broker Forum (TBF) search engine — executes part searches via browser.

TBF is a Vue SPA. The parts search is a plain GET route — no form fill needed:
navigate to ``/parts?query=<urlencoded mpn>`` and wait for EITHER the results
table (``table.table-fixed`` containing ``tr.hover-higlight-anchor``) OR a short
settle timeout for the no-results case. Then capture ``page.content()``.

Called by: worker loop
Depends on: session_manager (page)
"""

import asyncio
import time
from urllib.parse import quote_plus

from loguru import logger

# TBF parts search is a plain GET route (Vue SPA, no form submit).
SEARCH_BASE_URL = "https://www.thebrokersite.com/parts"

# Results table selector — a results table contains data rows.
RESULTS_SELECTOR = "table.table-fixed tr.hover-higlight-anchor"

# Seconds to settle for the no-results case when the results table never renders.
_NO_RESULTS_SETTLE_SECONDS = 6


async def search_part(page, part_number: str) -> dict:
    """Search for a part on TBF and capture the results HTML.

    Navigates directly to the GET search route, waits for the results table (or a
    short settle timeout for the no-results case), and captures the page HTML.

    Returns ``{"html": str, "url": str, "duration_ms": int, "status_code": int}``.
    """
    start = time.monotonic()

    url = f"{SEARCH_BASE_URL}?query={quote_plus(part_number)}"
    logger.info("TBF search: navigating to '{}'", url)

    status_code = 200
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if response is not None:
        try:
            status_code = response.status
        except Exception:
            status_code = 200

    # Wait for EITHER a results table OR the no-results settle timeout.
    try:
        await page.wait_for_selector(RESULTS_SELECTOR, timeout=_NO_RESULTS_SETTLE_SECONDS * 1000)
    except Exception:
        logger.debug("TBF search: no results table within {}s — treating as no-results", _NO_RESULTS_SETTLE_SECONDS)
        await asyncio.sleep(1)

    html = await page.content()

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info("TBF search: '{}' completed in {}ms (status={})", part_number, duration_ms, status_code)

    return {
        "html": html or "",
        "url": page.url,
        "duration_ms": duration_ms,
        "status_code": status_code,
    }
