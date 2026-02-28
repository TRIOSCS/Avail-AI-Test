"""ICsource search engine — executes part searches via browser.

Uses the ICsource ASP.NET WebForms search page. Fills the part number
field, clicks the search button, and waits for results to render.

Called by: worker loop
Depends on: session_manager (page), human_behavior
"""

import asyncio
import time

from loguru import logger

from .human_behavior import HumanBehavior

# ICsource member search URL
SEARCH_URL = "https://www.icsource.com/members/Search/NewSearch.aspx"


async def search_part(page, part_number: str) -> dict:
    """Search for a part on ICsource using form-based search.

    Navigates to the search page, fills the part number field, clicks
    the search button, and waits for results to render.
    Returns {"html": str, "total_count": int, "url": str, "duration_ms": int}.
    """
    start = time.monotonic()

    logger.info("ICS search: navigating to search for '{}'", part_number)
    await page.goto(SEARCH_URL, wait_until="load", timeout=30000)
    await asyncio.sleep(1)

    # Fill part number field
    pn_input = page.locator("#ctl00_ctl00_rtxtPartNumber2025")
    try:
        await pn_input.wait_for(timeout=10000)
    except Exception:
        # Fallback to multi-search field
        pn_input = page.locator("#ctl00_ctl00_txtPNZX")
        await pn_input.wait_for(timeout=5000)

    await pn_input.fill("")
    await HumanBehavior.human_type(page, pn_input, part_number)

    await HumanBehavior.random_delay(0.3, 0.8)

    # Click search button
    search_btn = page.locator("#ctl00_ctl00_btnNavSearch2025")
    await HumanBehavior.human_click(page, search_btn)

    # Wait for results to render
    try:
        await page.wait_for_selector(
            ".browseMatchItem, .tblWTBPanel, .noResultsMsg, .divDateGroup",
            timeout=20_000,
        )
    except Exception:
        logger.warning("ICS search: results selector not found within 20s — page may have changed structure")

    await asyncio.sleep(1)

    # Get results HTML
    html = await page.evaluate("""
        () => {
            const container = document.querySelector('.tblWTBPanel, .browseMatchItem, #searchResults, .search-results');
            if (container) {
                // Get the parent container that holds all results
                let parent = container.closest('.tblWTBPanel') || container.parentElement;
                return parent ? parent.outerHTML : container.outerHTML;
            }
            return document.body.innerHTML;
        }
    """)

    # Try to get total count from page
    total_count = await page.evaluate("""
        () => {
            const items = document.querySelectorAll('.browseMatchItem');
            return items ? items.length : 0;
        }
    """)

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info("ICS search: '{}' completed in {}ms, count={}", part_number, duration_ms, total_count)

    return {
        "html": html or "",
        "total_count": total_count,
        "url": page.url,
        "duration_ms": duration_ms,
    }
