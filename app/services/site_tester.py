"""Playwright site tester -- exhaustive click-every-button sweep of the app.

Crawls every view in the application using a headless Chromium browser,
clicks every visible button (skipping destructive actions like delete/remove/logout),
and captures console errors, network failures, and slow page loads.

Called by: routers/trouble_tickets.py (admin-only endpoint), scheduler (optional)
Depends on: patchright (Playwright-compatible), models/trouble_ticket.py,
            services/trouble_ticket_service.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

TEST_AREAS: list[dict[str, str]] = [
    {"name": "search", "hash": "#rfqs", "description": "Part number search and sourcing results"},
    {"name": "vendors", "hash": "#vendors", "description": "Vendor intelligence and cards"},
    {"name": "materials", "hash": "#materials", "description": "AI material tagging dashboard"},
    {"name": "customers", "hash": "#customers", "description": "CRM company management"},
    {"name": "contacts", "hash": "#contacts", "description": "CRM contact management"},
    {"name": "prospecting", "hash": "#suggested", "description": "Discovery pool and prospecting"},
    {"name": "dashboard", "hash": "#dashboard", "description": "Sourcing pipeline overview"},
    {"name": "scorecard", "hash": "#scorecard", "description": "Vendor scorecard and performance"},
    {"name": "proactive", "hash": "#proactive", "description": "Proactive offers management"},
    {"name": "offers", "hash": "#offers", "description": "Vendor offers and quotes"},
    {"name": "buyplans", "hash": "#buyplans", "description": "Buy plans management"},
    {"name": "alerts", "hash": "#alerts", "description": "Alerts and notifications view"},
    {"name": "settings", "hash": "#settings", "description": "Admin settings and configuration"},
    {"name": "tickets", "hash": "#tickets", "description": "Trouble tickets and self-heal"},
    {"name": "apihealth", "hash": "#apihealth", "description": "API health monitoring dashboard"},
]

# Buttons containing these words are skipped to avoid destructive actions
_DESTRUCTIVE_KEYWORDS = {"delete", "remove", "logout", "sign out", "signout", "destroy", "purge"}

# Pages taking longer than this (ms) are flagged as slow
_SLOW_THRESHOLD_MS = 3000


class SiteTester:
    """Headless browser tester that sweeps every view and clicks every button."""

    def __init__(self, base_url: str, session_cookie: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie
        self.issues: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []

    def record_issue(
        self,
        area: str,
        title: str,
        description: str,
        url: str | None = None,
        screenshot_b64: str | None = None,
        network_errors: list[dict] | None = None,
        console_errors: list[str] | None = None,
        performance_ms: float | None = None,
    ) -> None:
        """Append an issue found during the sweep."""
        self.issues.append({
            "area": area,
            "title": title,
            "description": description,
            "url": url or self.base_url,
            "screenshot_b64": screenshot_b64,
            "network_errors": network_errors or [],
            "console_errors": console_errors or [],
            "performance_ms": performance_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def run_full_sweep(self) -> list[dict[str, Any]]:
        """Launch headless Chromium, visit every area, click every button, collect issues."""
        from patchright.async_api import async_playwright

        logger.info("site_tester: starting full sweep of {} areas", len(TEST_AREAS))

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})

            # Set auth cookie
            await context.add_cookies([{
                "name": "session",
                "value": self.session_cookie,
                "url": self.base_url,
            }])

            page = await context.new_page()

            # Collect console errors and network failures per-area
            console_errors: list[str] = []
            network_errors: list[dict[str, Any]] = []

            page.on("console", lambda msg: (
                console_errors.append(f"[{msg.type}] {msg.text}")
                if msg.type in ("error", "warning") else None
            ))
            page.on("requestfailed", lambda req: network_errors.append({
                "url": req.url,
                "method": req.method,
                "failure": req.failure,
            }))

            for area in TEST_AREAS:
                # Clear per-area error collectors
                console_errors.clear()
                network_errors.clear()

                self.progress.append({"area": area["name"], "status": "testing"})
                logger.info("site_tester: testing area '{}'", area["name"])

                try:
                    await self._test_area(page, area, console_errors, network_errors)
                except Exception as exc:
                    self.record_issue(
                        area=area["name"],
                        title=f"Exception testing {area['name']}",
                        description=str(exc),
                        url=f"{self.base_url}/{area['hash']}",
                    )
                    logger.warning("site_tester: exception in area '{}': {}", area["name"], exc)

                self.progress[-1]["status"] = "done"

            await browser.close()

        logger.info("site_tester: sweep complete — {} issues found", len(self.issues))
        return self.issues

    async def _test_area(
        self,
        page: Any,
        area: dict[str, str],
        console_errors: list[str],
        network_errors: list[dict[str, Any]],
    ) -> None:
        """Navigate to a single area, check load time, and click all safe buttons."""
        url = f"{self.base_url}/{area['hash']}"
        start = datetime.now(timezone.utc)

        await page.goto(url, wait_until="networkidle", timeout=15000)

        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

        # Flag slow loads
        if elapsed_ms > _SLOW_THRESHOLD_MS:
            self.record_issue(
                area=area["name"],
                title=f"Slow page load: {area['name']}",
                description=f"Page took {elapsed_ms:.0f}ms to reach networkidle (threshold: {_SLOW_THRESHOLD_MS}ms)",
                url=url,
                performance_ms=elapsed_ms,
            )

        # Capture any console/network errors from initial load
        if console_errors:
            self.record_issue(
                area=area["name"],
                title=f"Console errors on load: {area['name']}",
                description=f"{len(console_errors)} console error(s) on initial load",
                url=url,
                console_errors=list(console_errors),
            )

        if network_errors:
            self.record_issue(
                area=area["name"],
                title=f"Network errors on load: {area['name']}",
                description=f"{len(network_errors)} failed network request(s) on initial load",
                url=url,
                network_errors=list(network_errors),
            )

        # Find and click all visible buttons (skip destructive ones)
        buttons = await page.query_selector_all("button:visible, [role='button']:visible, a.btn:visible")

        for btn in buttons:
            btn_text = (await btn.text_content() or "").strip().lower()

            # Skip destructive buttons
            if any(kw in btn_text for kw in _DESTRUCTIVE_KEYWORDS):
                logger.debug("site_tester: skipping destructive button '{}' in {}", btn_text, area["name"])
                continue

            # Clear errors before click
            console_errors.clear()
            network_errors.clear()

            try:
                await btn.click(timeout=3000)
                # Brief wait for any resulting network activity
                await page.wait_for_timeout(500)
            except Exception as click_exc:
                self.record_issue(
                    area=area["name"],
                    title=f"Button click failed: '{btn_text}' in {area['name']}",
                    description=str(click_exc),
                    url=url,
                )
                continue

            # Check for errors after click
            if console_errors:
                self.record_issue(
                    area=area["name"],
                    title=f"Console error after clicking '{btn_text}' in {area['name']}",
                    description=f"{len(console_errors)} console error(s) after button click",
                    url=url,
                    console_errors=list(console_errors),
                )

            if network_errors:
                self.record_issue(
                    area=area["name"],
                    title=f"Network error after clicking '{btn_text}' in {area['name']}",
                    description=f"{len(network_errors)} failed request(s) after button click",
                    url=url,
                    network_errors=list(network_errors),
                )


async def create_tickets_from_issues(issues: list[dict[str, Any]], db: Any) -> int:
    """Create a TroubleTicket for each issue found by the site tester.

    Deduplicates: skips issues where an open ticket already exists
    in the same area with the same title prefix (first 80 chars).

    Returns the number of tickets created.
    """
    from app.models.trouble_ticket import TroubleTicket
    from app.services.trouble_ticket_service import create_ticket

    open_statuses = ("submitted", "diagnosed", "escalated", "in_progress", "open", "fix_queued")
    existing = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status.in_(open_statuses))
        .filter(TroubleTicket.source == "playwright")
        .all()
    )
    seen = {(t.current_view or "", (t.title or "")[:80]) for t in existing}

    count = 0
    for issue in issues:
        area = issue.get("area", "")
        title = issue.get("title", "")[:200]
        dedup_key = (area, title[:80])

        if dedup_key in seen:
            logger.debug("site_tester: skipping duplicate '{}' in '{}'", title[:60], area)
            continue

        try:
            create_ticket(
                db=db,
                user_id=1,  # system user
                title=title,
                description=issue["description"],
                current_page=issue.get("url"),
                source="playwright",
                console_errors="\n".join(issue.get("console_errors", [])) or None,
                current_view=area,
            )
            seen.add(dedup_key)
            count += 1
        except Exception as exc:
            logger.warning("site_tester: failed to create ticket for '{}': {}", title, exc)

    if count:
        db.commit()
        logger.info("site_tester: created {} trouble tickets from sweep issues", count)

    return count
