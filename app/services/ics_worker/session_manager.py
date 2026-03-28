"""ICsource browser session manager.

Manages a persistent Chrome browser session via Patchright (undetected
Playwright fork) running inside an Xvfb virtual display. Handles login,
session health checks, and automatic re-authentication.

ICsource uses Telerik AJAX (ASP.NET WebForms) — login must click the
button rather than pressing Enter, and the password field is revealed
by clicking a placeholder.

Called by: worker loop
Depends on: patchright, human_behavior, config
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Page

from loguru import logger

from .config import IcsConfig
from .human_behavior import HumanBehavior


class IcsSessionManager:
    """Manages the ICsource browser session lifecycle."""

    def __init__(self, config: IcsConfig):
        self.config = config
        self._playwright = None
        self._context = None
        self._page: Page | None = None
        self.is_logged_in = False

    @property
    def page(self):
        return self._page

    async def start(self):
        """Launch browser with persistent context and navigate to ICsource."""
        if not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "DISPLAY environment variable not set. "
                "Patchright requires Xvfb (e.g. DISPLAY=:99). "
                "Ensure avail-xvfb.service is running."
            )

        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.ICS_BROWSER_PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self._page = self._context.pages[0]
        await self._page.goto("https://www.icsource.com/")
        await asyncio.sleep(2)

        self.is_logged_in = await self.check_session_health()
        if self.is_logged_in:
            logger.info("ICS session: already logged in (persistent cookies)")
        else:
            logger.info("ICS session: not logged in, will need to authenticate")

    async def check_session_health(self) -> bool:
        """Check if the ICS session is still valid.

        Navigates to the member search page and checks we're not redirected to login or
        the public home page.
        """
        try:
            await self._page.goto(
                "https://www.icsource.com/members/Search/NewSearch.aspx",
                wait_until="load",
                timeout=20000,
            )
            await asyncio.sleep(3)
            url = self._page.url.lower()
            logger.debug("ICS health check URL: {}", url)
            # If redirected to login page, session is expired
            if "login" in url:
                return False
            # Must be on a members page (not redirected to public home)
            if "/members/" not in url:
                logger.debug("ICS health check: redirected to {}", url)
                return False
            return True
        except Exception as e:
            logger.warning("ICS session health check failed: {}", e)
            return False

    async def login(self) -> bool:
        """Log in to ICsource.

        ICsource login flow:
        1. Navigate to /home/LogIn.aspx
        2. Set username via JS (Telerik RadTextBox doesn't respond to keyboard.type)
        3. Click password placeholder to reveal real password input
        4. Set password via JS
        5. Click the styled green Log In div button
        """
        if not self.config.ICS_USERNAME or not self.config.ICS_PASSWORD:
            logger.error("ICS login: ICS_USERNAME or ICS_PASSWORD not configured")
            return False

        try:
            await self._page.goto("https://www.icsource.com/home/LogIn.aspx")
            await asyncio.sleep(2)

            # Wait for login form
            username_sel = "#ctl00_ctl00_body_bodycontent_logincontrol_txtUserName"
            await self._page.locator(username_sel).wait_for(timeout=10000)

            # Set username via JS evaluate (Telerik inputs ignore keyboard events)
            await self._page.evaluate(
                """(u) => {
                    const el = document.getElementById(
                        'ctl00_ctl00_body_bodycontent_logincontrol_txtUserName'
                    );
                    el.value = u;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                self.config.ICS_USERNAME,
            )

            await HumanBehavior.random_delay(0.3, 0.8)

            # Click password placeholder to reveal real input
            pwd_placeholder = self._page.locator(".passwordhidden")
            try:
                await pwd_placeholder.wait_for(timeout=3000)
                await pwd_placeholder.click()
                await asyncio.sleep(0.5)
            except Exception:
                pass  # Placeholder may not exist on all login page versions

            # Set password via JS evaluate
            await self._page.evaluate(
                """(p) => {
                    const el = document.getElementById(
                        'ctl00_ctl00_body_bodycontent_logincontrol_txtPassword'
                    );
                    el.style.display = 'inline-block';
                    el.value = p;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                self.config.ICS_PASSWORD,
            )

            await HumanBehavior.random_delay(0.5, 1.0)

            # Click the styled green Log In button (not the hidden ASP.NET input)
            login_btn = self._page.locator("div.button.green:has-text('Log In')")
            try:
                await login_btn.wait_for(state="visible", timeout=5000)
            except Exception:
                login_btn = self._page.locator("#ctl00_ctl00_body_bodycontent_logincontrol_btnLogIn")
            await HumanBehavior.human_click(self._page, login_btn)
            await asyncio.sleep(5)

            # Verify login succeeded
            self.is_logged_in = await self.check_session_health()
            if self.is_logged_in:
                logger.info("ICS login: success")
            else:
                logger.error("ICS login: failed — session not authorized after submit")
            return self.is_logged_in

        except Exception as e:
            logger.error("ICS login: exception during login: {}", e)
            self.is_logged_in = False
            return False

    async def ensure_session(self) -> bool:
        """Ensure we have a valid session, re-logging in if needed."""
        if await self.check_session_health():
            self.is_logged_in = True
            return True
        logger.info("ICS session expired, re-authenticating...")
        return await self.login()

    async def stop(self):
        """Close browser context and stop Playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("ICS session stop error: {}", e)
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            self.is_logged_in = False
