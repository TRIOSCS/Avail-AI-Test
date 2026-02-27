"""NetComponents browser session manager.

Manages a persistent Chrome browser session via Patchright (undetected
Playwright fork) running inside an Xvfb virtual display. Handles login,
session health checks, and automatic re-authentication.

Called by: worker loop
Depends on: patchright, human_behavior, config
"""

import asyncio
import os

from loguru import logger

from .config import NcConfig
from .human_behavior import HumanBehavior


class NcSessionManager:
    """Manages the NetComponents browser session lifecycle."""

    def __init__(self, config: NcConfig):
        self.config = config
        self._playwright = None
        self._context = None
        self._page = None
        self.is_logged_in = False

    @property
    def page(self):
        return self._page

    async def start(self):
        """Launch browser with persistent context and navigate to NC."""
        if not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "DISPLAY environment variable not set. "
                "Patchright requires Xvfb (e.g. DISPLAY=:99). "
                "Ensure avail-xvfb.service is running."
            )

        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.NC_BROWSER_PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self._page = self._context.pages[0]
        await self._page.goto("https://www.netcomponents.com/")
        await asyncio.sleep(2)

        self.is_logged_in = await self.check_session_health()
        if self.is_logged_in:
            logger.info("NC session: already logged in (persistent cookies)")
        else:
            logger.info("NC session: not logged in, will need to authenticate")

    async def check_session_health(self) -> bool:
        """Check if the NC session is still valid via /client/isauthorized."""
        try:
            result = await self._page.evaluate("""
                async () => {
                    const r = await fetch('/client/isauthorized', {credentials: 'same-origin'});
                    return {status: r.status, body: await r.text()};
                }
            """)
            is_auth = result.get("status") == 200 and "true" in result.get("body", "").lower()
            return is_auth
        except Exception as e:
            logger.warning("NC session health check failed: {}", e)
            return False

    async def login(self) -> bool:
        """Log in to NetComponents using human-like typing."""
        if not self.config.NC_USERNAME or not self.config.NC_PASSWORD:
            logger.error("NC login: NC_USERNAME or NC_PASSWORD not configured")
            return False

        try:
            await self._page.goto("https://www.netcomponents.com/account/login")
            await asyncio.sleep(2)

            # Wait for login form
            email_input = self._page.locator('input[name="Email"], input[type="email"], #Email')
            await email_input.wait_for(timeout=10000)

            # Clear and type username
            await email_input.fill("")
            await HumanBehavior.human_type(self._page, email_input, self.config.NC_USERNAME)

            await HumanBehavior.random_delay(0.3, 0.8)

            # Type password
            pwd_input = self._page.locator('input[name="Password"], input[type="password"], #Password')
            await pwd_input.fill("")
            await HumanBehavior.human_type(self._page, pwd_input, self.config.NC_PASSWORD)

            await HumanBehavior.random_delay(0.5, 1.0)

            # Submit with Enter (more natural than clicking button)
            await self._page.keyboard.press("Enter")
            await asyncio.sleep(3)

            # Verify login succeeded
            self.is_logged_in = await self.check_session_health()
            if self.is_logged_in:
                logger.info("NC login: success")
            else:
                logger.error("NC login: failed — session not authorized after submit")
            return self.is_logged_in

        except Exception as e:
            logger.error("NC login: exception during login: {}", e)
            self.is_logged_in = False
            return False

    async def ensure_session(self) -> bool:
        """Ensure we have a valid session, re-logging in if needed."""
        if await self.check_session_health():
            self.is_logged_in = True
            return True
        logger.info("NC session expired, re-authenticating...")
        return await self.login()

    async def stop(self):
        """Close browser context and stop Playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("NC session stop error: {}", e)
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            self.is_logged_in = False
