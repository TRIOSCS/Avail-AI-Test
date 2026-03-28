"""ICsource search engine — executes part searches via browser.

Uses the ICsource ASP.NET WebForms search page. Fills the part number
field, clicks the search button, and waits for results to render.

Called by: worker loop
Depends on: session_manager (page), human_behavior
"""

import asyncio
import tempfile
import time

from loguru import logger

from .human_behavior import HumanBehavior

# ICsource member search URL
SEARCH_URL = "https://www.icsource.com/members/Search/NewSearch.aspx"


async def search_part(page, part_number: str) -> dict:
    """Search for a part on ICsource using form-based search.

    Navigates to the search page, fills the part number field, clicks the search button,
    and waits for results to render. Returns {"html": str, "total_count": int, "url":
    str, "duration_ms": int}.
    """
    start = time.monotonic()

    logger.info("ICS search: navigating to search for '{}'", part_number)
    await page.goto(SEARCH_URL, wait_until="load", timeout=30000)
    await asyncio.sleep(1)

    # Fill part number field — try multiple selectors in priority order
    pn_input = None
    for selector in [
        "#ctl00_ctl00_rtxtPartNumber2025",
        "#ctl00_ctl00_txtPNZX",
        "input[id*='PartNumber']",
        "input[id*='txtPN']",
    ]:
        loc = page.locator(selector)
        try:
            await loc.wait_for(timeout=5000)
            if await loc.is_visible():
                pn_input = loc
                logger.debug("ICS search: using input '{}'", selector)
                break
        except Exception:
            continue

    if not pn_input:
        # Last resort: find any visible text input on the page
        pn_input = page.locator("input[type='text']").first
        logger.warning("ICS search: using first visible text input as fallback")

    await pn_input.fill("")
    await HumanBehavior.human_type(page, pn_input, part_number)

    await HumanBehavior.random_delay(0.3, 0.8)

    # Diagnostic: log all buttons/inputs that could be search triggers
    page_info = await page.evaluate("""
        () => {
            const buttons = Array.from(document.querySelectorAll('input[type="submit"], input[type="button"], button, a.btn, [onclick]'));
            const info = buttons.slice(0, 20).map(el => ({
                tag: el.tagName,
                id: el.id || '',
                type: el.type || '',
                value: el.value || '',
                text: (el.textContent || '').trim().substring(0, 50),
                visible: el.offsetParent !== null,
                display: getComputedStyle(el).display,
                onclick: (el.getAttribute('onclick') || '').substring(0, 100),
            }));
            const forms = Array.from(document.querySelectorAll('form')).map(f => ({
                id: f.id || '',
                action: f.action || '',
                method: f.method || '',
            }));
            return { buttons: info, forms: forms, url: location.href, title: document.title };
        }
    """)
    logger.info("ICS search: page diagnostic — url={}, title={}", page_info.get("url"), page_info.get("title"))
    for btn in page_info.get("buttons", []):
        logger.debug(
            "ICS search: found element: tag={} id='{}' type={} value='{}' text='{}' visible={} display={} onclick='{}'",
            btn["tag"],
            btn["id"],
            btn["type"],
            btn["value"],
            btn["text"],
            btn["visible"],
            btn["display"],
            btn["onclick"],
        )
    for form in page_info.get("forms", []):
        logger.debug(
            "ICS search: found form: id='{}' action='{}' method='{}'", form["id"], form["action"], form["method"]
        )

    # Take a diagnostic screenshot
    try:
        _tmp = tempfile.gettempdir()
        _screenshot_path = f"{_tmp}/ics_search_debug.png"
        await page.screenshot(path=_screenshot_path)
        logger.info("ICS search: saved diagnostic screenshot to {}", _screenshot_path)
    except Exception as e:
        logger.warning("ICS search: screenshot failed: {}", e)

    # Submit search — try multiple strategies
    submitted = False

    # Strategy 1: Click visible search button
    for btn_selector in [
        "#ctl00_ctl00_btnNavSearch2025",
        "input[id*='btnNavSearch']",
        "input[id*='btnSearch']",
        "button[id*='Search']",
        "a[id*='Search']",
        "a[id*='btnNav']",
    ]:
        btn = page.locator(btn_selector)
        try:
            if await btn.count() > 0 and await btn.is_visible():
                await HumanBehavior.human_click(page, btn)
                submitted = True
                logger.debug("ICS search: clicked visible button '{}'", btn_selector)
                break
        except Exception:
            continue

    # Strategy 2: Force-click the hidden button (bypasses visibility check)
    if not submitted:
        for btn_selector in [
            "#ctl00_ctl00_btnNavSearch2025",
            "input[id*='btnNavSearch']",
            "input[id*='btnSearch']",
        ]:
            btn = page.locator(btn_selector)
            try:
                if await btn.count() > 0:
                    await btn.click(force=True, timeout=5000)
                    submitted = True
                    logger.info("ICS search: force-clicked hidden button '{}'", btn_selector)
                    break
            except Exception as e:
                logger.warning("ICS search: force-click '{}' failed: {}", btn_selector, e)
                continue

    # Strategy 3: JS click + showPageAjax() for ASP.NET WebForms
    if not submitted:
        logger.info("ICS search: triggering search via JavaScript")
        js_result = await page.evaluate("""
            () => {
                // Try multiple button selectors
                const selectors = [
                    "#ctl00_ctl00_btnNavSearch2025",
                    "[id*='btnNavSearch']",
                    "[id*='btnSearch']",
                    "[id*='Search'][type='submit']",
                    "[id*='Search'][type='image']",
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) {
                        btn.click();
                        return "clicked: " + sel + " (id=" + btn.id + ")";
                    }
                }
                // Try ASP.NET postback functions
                if (typeof showPageAjax === 'function') {
                    showPageAjax();
                    return "called showPageAjax()";
                }
                if (typeof __doPostBack === 'function') {
                    // Try common postback targets
                    try { __doPostBack('ctl00$ctl00$btnNavSearch2025', ''); return "postback btnNavSearch2025"; } catch(e) {}
                    try { __doPostBack('ctl00$ctl00$lnkNavSearch2025', ''); return "postback lnkNavSearch2025"; } catch(e) {}
                }
                // Try submitting the form directly
                const form = document.querySelector('form');
                if (form) {
                    form.submit();
                    return "submitted form";
                }
                return "no method found";
            }
        """)
        logger.info("ICS search: JS strategy result: {}", js_result)

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
