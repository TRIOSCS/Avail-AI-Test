"""The Broker Forum (TBF) search engine — PHASE 1 STUB.

Executes a part search via the logged-in browser page: navigates to the
search route, fills the part-number field, submits, waits for results, and
captures the results ``outerHTML``. The search route + field/submit/results
selectors require a logged-in capture, so they are stubbed: ``search_part``
raises ``NotImplementedError`` until Phase 2 encodes them. The module imports
cleanly so the worker package is importable.

Called by: worker loop
Depends on: session_manager (page), human_behavior
"""

import time

from loguru import logger

from .human_behavior import HumanBehavior  # noqa: F401  (used once selectors land in Phase 2)

# TODO(phase2): real selector from logged-in capture — the authenticated
# member search URL on thebrokersite.com.
SEARCH_URL = ""


async def search_part(page, part_number: str) -> dict:
    """Search for a part on TBF and capture the results HTML.

    Returns ``{"html": str, "url": str, "duration_ms": int, "status_code": int}``.

    PHASE 1: raises ``NotImplementedError`` — the search route, part-number
    field, submit control, and results-wait selectors are unknown until a
    logged-in capture exists. The timing/return scaffold below is the Phase-2
    shape the worker loop already consumes.
    """
    start = time.monotonic()
    logger.info("TBF search: (phase-1 stub) requested search for '{}'", part_number)

    # TODO(phase2): real selector from logged-in capture.
    #   1. await page.goto(SEARCH_URL, wait_until="load", timeout=30000)
    #   2. fill the part-number field via HumanBehavior.human_type(...)
    #   3. submit (click the search control)
    #   4. await page.wait_for_selector(<results-container>, timeout=20000)
    #   5. html = await page.evaluate("() => <results-container>.outerHTML")
    #   6. status_code from the navigation response
    raise NotImplementedError("phase2: selectors")

    # Unreachable in Phase 1 — documents the return contract for Phase 2.
    duration_ms = int((time.monotonic() - start) * 1000)  # pragma: no cover
    return {  # pragma: no cover
        "html": "",
        "url": page.url,
        "duration_ms": duration_ms,
        "status_code": 200,
    }
