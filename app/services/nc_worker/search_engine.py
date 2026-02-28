"""NetComponents search engine — hybrid HTTP + browser.

Tries HTTP GET first (fast, ~150ms). If the response lacks results
(e.g. during maintenance mode when API is blocked), falls back to
browser-based search via Patchright (~5-10s but always works).

Called by: worker loop
Depends on: session_manager, result_parser (for validation)
"""

import asyncio
import time
from urllib.parse import quote

from loguru import logger


def build_search_url(mpn: str, search_logic: str = "Begins") -> str:
    """Build the full NC search URL with all required parameters.

    Includes duplicate Filters/PSA params (ASP.NET MVC checkbox binding).
    """
    encoded_mpn = quote(mpn, safe="")
    return (
        f"https://www.netcomponents.com/search/result"
        f"?SearchId=0&SortBy=0&Demo=false&SearchType=0&SearchLogic={search_logic}"
        f"&Filters=true&Filters=false&PSA=true&PSA=false"
        f"&PartsSearched%5B0%5D.PartNumber={encoded_mpn}"
        f"&PartsSearched%5B1%5D.PartNumber="
        f"&PartsSearched%5B2%5D.PartNumber="
        f"&MultiSearchParts="
    )


def search_part(session_manager, part_number: str) -> dict:
    """Search for a part — tries HTTP first, falls back to browser.

    Returns {"html": str, "url": str, "duration_ms": int, "status_code": int, "mode": str}.
    """
    # Try HTTP first (fast path)
    result = _search_http(session_manager, part_number)

    # Check if we got actual results (look for result markers in HTML)
    if _has_results(result["html"]):
        result["mode"] = "http"
        return result

    # HTTP didn't return results — try browser fallback
    logger.info("NC search: HTTP returned no results, trying browser fallback for '{}'", part_number)
    browser_result = asyncio.run(_search_browser(session_manager, part_number))
    if browser_result:
        browser_result["mode"] = "browser"
        return browser_result

    # Both failed — return the HTTP result (may be empty)
    result["mode"] = "http_empty"
    return result


def _search_http(session_manager, part_number: str) -> dict:
    """Search via HTTP GET — fast but may not work during maintenance."""
    url = build_search_url(part_number)
    start = time.monotonic()

    try:
        resp = session_manager.session.get(url, timeout=60)
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "NC search (HTTP): '{}' in {}ms (status={}, size={}KB)",
            part_number, duration_ms, resp.status_code, len(resp.text) // 1024,
        )

        return {
            "html": resp.text,
            "url": url,
            "duration_ms": duration_ms,
            "status_code": resp.status_code,
        }
    except Exception as e:
        logger.warning("NC search (HTTP): failed for '{}': {}", part_number, e)
        return {"html": "", "url": url, "duration_ms": 0, "status_code": 0}


async def _search_browser(session_manager, part_number: str) -> dict | None:
    """Search via browser automation — slower but works during maintenance."""
    try:
        if not session_manager.has_browser:
            await session_manager.start_browser()

        if not session_manager.has_browser:
            logger.error("NC search (browser): could not start browser")
            return None

        # Navigate to NC and login via browser if needed
        page = session_manager.page
        await page.goto("https://www.netcomponents.com/", wait_until="load", timeout=30000)
        await asyncio.sleep(2)

        auth_check = await page.evaluate("""
            async () => {
                const r = await fetch('/client/isauthorized', {credentials: 'same-origin'});
                return r.status === 200 && (await r.text()).toLowerCase().includes('true');
            }
        """)
        if not auth_check:
            if not await session_manager.login_browser():
                logger.error("NC search (browser): login failed")
                return None

        url = build_search_url(part_number)
        start = time.monotonic()

        await page.goto(url, wait_until="load", timeout=30000)

        # Wait for results to render (NC uses async JS polling)
        try:
            await page.wait_for_selector(
                "table.searchresultstable, .no-results, .search-total-lines",
                timeout=25_000,
            )
        except Exception:
            logger.warning("NC search (browser): results selector not found within 25s")

        # Extra wait for async result loading
        await asyncio.sleep(3)

        # Get the full page HTML (results are rendered by JS into the DOM)
        html = await page.evaluate("() => document.documentElement.outerHTML")

        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "NC search (browser): '{}' in {}ms (size={}KB)",
            part_number, duration_ms, len(html) // 1024,
        )

        return {
            "html": html,
            "url": url,
            "duration_ms": duration_ms,
            "status_code": 200,
        }

    except Exception as e:
        logger.error("NC search (browser): failed for '{}': {}", part_number, e)
        return None


def _has_results(html: str) -> bool:
    """Quick check if the HTML contains search result indicators."""
    if not html:
        return False
    # Look for result table markers in the HTML
    markers = [
        "searchresultstable",
        "div-table-float-reg",
        "floating-block",
        "region-header",
    ]
    html_lower = html.lower()
    return any(m in html_lower for m in markers)
