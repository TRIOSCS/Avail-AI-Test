"""The Broker Forum (TBF) browser session manager — PHASE 1 STUB.

Manages a persistent Chrome browser session via Patchright (undetected
Playwright fork) running inside an Xvfb virtual display. Handles login,
session health checks, and automatic re-authentication.

The login flow, the auth/session marker, and the member-area URL require a
logged-in capture, so ``login`` and ``check_session_health`` are stubbed
(raise ``NotImplementedError``) until Phase 2. The browser lifecycle
(``start``/``stop``) and the DISPLAY guard are real so the module imports and
the worker is importable.

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

from .config import TbfConfig
from .human_behavior import HumanBehavior  # noqa: F401  (used once selectors land in Phase 2)

# TODO(phase2): real selector from logged-in capture — the TBF home/login URL.
HOME_URL = "https://www.thebrokersite.com/"


class TbfSessionManager:
    """Manages The Broker Forum browser session lifecycle."""

    def __init__(self, config: TbfConfig):
        self.config = config
        self._playwright = None
        self._context = None
        self._page: Page | None = None
        self.is_logged_in = False

    @property
    def page(self):
        return self._page

    async def start(self):
        """Launch browser with persistent context and navigate to TBF."""
        if not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "DISPLAY environment variable not set. "
                "Patchright requires Xvfb (e.g. DISPLAY=:99). "
                "Ensure avail-xvfb.service is running."
            )

        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.TBF_BROWSER_PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self._page = self._context.pages[0]
        await self._page.goto(HOME_URL)
        await asyncio.sleep(2)

        self.is_logged_in = await self.check_session_health()
        if self.is_logged_in:
            logger.info("TBF session: already logged in (persistent cookies)")
        else:
            logger.info("TBF session: not logged in, will need to authenticate")

    async def check_session_health(self) -> bool:
        """Check if the TBF session is still valid.

        PHASE 1: raises ``NotImplementedError`` — the authenticated member-area
        URL and the logged-in marker are unknown until a capture exists.
        """
        # TODO(phase2): real selector from logged-in capture.
        # Navigate to a member-only page and assert we are not bounced to login;
        # check for an authenticated marker (e.g. a logout link / account menu).
        raise NotImplementedError("phase2: selectors")

    async def login(self) -> bool:
        """Log in to The Broker Forum with member credentials.

        PHASE 1: raises ``NotImplementedError`` — the login form field/submit
        selectors are unknown until a capture exists.
        """
        if not self.config.TBF_USERNAME or not self.config.TBF_PASSWORD:
            logger.error("TBF login: TBF_USERNAME or TBF_PASSWORD not configured")
            return False

        # TODO(phase2): real selector from logged-in capture.
        # Navigate to the login form, fill username + password via
        # HumanBehavior, submit, then verify via check_session_health().
        raise NotImplementedError("phase2: selectors")

    async def ensure_session(self) -> bool:
        """Ensure we have a valid session, re-logging in if needed."""
        if await self.check_session_health():
            self.is_logged_in = True
            return True
        logger.info("TBF session expired, re-authenticating...")
        return await self.login()

    async def stop(self):
        """Close browser context and stop Playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("TBF session stop error: {}", e)
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            self.is_logged_in = False
