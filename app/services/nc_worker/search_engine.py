"""NetComponents search engine — executes part searches via browser.

Uses the NC search URL pattern with all required ASP.NET MVC parameters.
Handles both navigation-based and API-based search approaches.

Called by: worker loop
Depends on: session_manager (page), human_behavior
"""

import asyncio
import time
from urllib.parse import quote

from loguru import logger


def build_search_url(mpn: str) -> str:
    """Build the full NC search URL with all required parameters.

    Includes duplicate Filters/PSA params (ASP.NET MVC checkbox binding).
    """
    encoded_mpn = quote(mpn, safe="")
    return (
        f"https://www.netcomponents.com/search/result"
        f"?SearchId=0&SortBy=0&Demo=false&SearchType=0&SearchLogic=Begins"
        f"&Filters=true&Filters=false&PSA=true&PSA=false"
        f"&PartsSearched%5B0%5D.PartNumber={encoded_mpn}"
        f"&PartsSearched%5B1%5D.PartNumber="
        f"&PartsSearched%5B2%5D.PartNumber="
        f"&MultiSearchParts="
    )


async def search_part(page, part_number: str) -> dict:
    """Search for a part on NC using the navigation approach.

    Navigates to the search URL and waits for results to render.
    Returns {"html": str, "total_count": int, "url": str, "duration_ms": int}.
    """
    url = build_search_url(part_number)
    start = time.monotonic()

    logger.info("NC search: navigating to search for '{}'", part_number)
    await page.goto(url, wait_until="load", timeout=30000)

    # Wait for NC's async polling to complete and results to render.
    # NC uses: checkmasterid → startsearchapi → counttotalapiparts → getresult
    try:
        await page.wait_for_selector(
            "table.results-table, .no-results, #searchResults table, #divResult table, .search-results",
            timeout=20_000,
        )
    except Exception:
        logger.warning("NC search: results selector not found within 20s — page may have changed structure")

    # Get results HTML
    html = await page.evaluate("""
        () => {
            const container = document.querySelector('.search-results, #searchResults, .api-results, .result-table');
            return container ? container.innerHTML : document.body.innerHTML;
        }
    """)

    # Try to get total count from page
    total_count = await page.evaluate("""
        () => {
            const el = document.querySelector('.total-count, .result-count, #totalParts');
            if (el) return parseInt(el.textContent.replace(/,/g, ''), 10) || 0;
            return 0;
        }
    """)

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info("NC search: '{}' completed in {}ms, count={}", part_number, duration_ms, total_count)

    return {
        "html": html or "",
        "total_count": total_count,
        "url": url,
        "duration_ms": duration_ms,
    }


async def search_part_via_api(page, part_number: str) -> dict:
    """Fallback: execute NC search via direct API calls using page.evaluate().

    Uses the 5-step API flow: checkmasterid → startsearchapi → poll → getresult.
    Returns {"html": str, "total_count": int, "duration_ms": int}.
    """
    start = time.monotonic()
    encoded_mpn = quote(part_number, safe="")

    logger.info("NC search (API fallback): searching for '{}'", part_number)

    result = await page.evaluate(f"""
        async () => {{
            // Step 1: checkmasterid
            await fetch('/search/checkmasterid?masterID=0&p={encoded_mpn}&l=Begins',
                {{credentials: 'same-origin'}});

            // Step 2: wait
            await new Promise(r => setTimeout(r, 1500));

            // Step 3: start search
            await fetch('/search/startsearchapi', {{credentials: 'same-origin'}});

            // Step 4: poll for results
            let count = 0;
            for (let i = 0; i < 10; i++) {{
                await new Promise(r => setTimeout(r, 750));
                const cr = await fetch('/search/counttotalapiparts', {{credentials: 'same-origin'}});
                const ct = await cr.text();
                count = parseInt(ct, 10) || 0;
                if (count > 0) break;
            }}

            // Step 5: get results
            const rr = await fetch('/search/getresult', {{credentials: 'same-origin'}});
            const html = await rr.text();

            return {{html: html, total_count: count}};
        }}
    """)

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info("NC search (API): '{}' completed in {}ms, count={}", part_number, duration_ms, result.get("total_count", 0))

    return {
        "html": result.get("html", ""),
        "total_count": result.get("total_count", 0),
        "duration_ms": duration_ms,
    }
