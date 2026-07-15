"""The Broker Forum (TBF) browser session manager.

Manages a persistent Chrome browser session via Patchright (undetected
Playwright fork) running inside an Xvfb virtual display. Handles login,
session health checks, and automatic re-authentication.

TBF is a Vue SPA:
- ``networkidle`` TIMES OUT on the chatty SPA — use ``domcontentloaded`` + a
  fixed settle, never networkidle.
- The ``--window-size`` launch arg is MANDATORY: without it the no-WM Xvfb
  window is 0x0 and nothing lays out / is clickable.
- The logged-in marker is the PRESENCE of the "Sign out" control (rendered into
  the member nav on every authenticated page). Keying on a positive marker fails
  SAFE: an unrecognized page reads as logged-out and re-authenticates instead of
  silently writing anonymized rows. Do NOT key on "TBS Member" text — that is the
  *anonymized company label TBF shows to logged-OUT visitors* on listings.
- The login form is ``form:has(input[name='password'])`` — there is also a
  password-RESET form (email only) and a ``fakeEmail`` honeypot; we never touch
  those. The submit control is the form's ``button[type=submit]``.
- 2FA is currently NOT enforced on the account (TBF offers an optional "Setup
  2FA Now"); email+password logs straight in. If 2FA is later enabled an
  emailed-code step appears that an unattended worker cannot complete — login()
  detects and logs that case clearly.

Called by: worker loop
Depends on: patchright, config
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Page

from loguru import logger

from .config import TbfConfig

HOME_URL = "https://www.thebrokersite.com/"

# Logged-IN marker (POSITIVE, fail-safe): the "Sign out" control, rendered into
# the member nav (desktop dropdown + mobile menu) on every authenticated page and
# absent when logged out. We key on its PRESENCE rather than the ABSENCE of a
# "Sign In" button so that any selector/label drift fails SAFE — an unrecognized
# page reads as logged-out and triggers a re-login, instead of silently writing
# anonymized "TBS Member" rows. (Never key on "TBS Member" text — that is the
# anonymized company label TBF shows to logged-OUT visitors.)
LOGGED_IN_MARKER = "a:has-text('Sign out'), button:has-text('Sign out')"

# Cookie/consent banner buttons to try-dismiss (best effort, never fatal).
_CONSENT_BUTTON_NAMES = ("^Accept$", "^Dismiss$", "^Close$", "^I agree$")


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
            # MANDATORY: without --window-size the no-WM Xvfb window is 0x0 and
            # nothing lays out / is clickable.
            args=["--window-size=1920,1080", "--window-position=0,0"],
        )
        self._page = self._context.pages[0]
        await self._page.goto(HOME_URL, wait_until="domcontentloaded")
        await asyncio.sleep(4)

        self.is_logged_in = await self.check_session_health()
        if self.is_logged_in:
            logger.info("TBF session: already logged in (persistent cookies)")
        else:
            logger.info("TBF session: not logged in, will need to authenticate")

    async def _dismiss_consent_banner(self):
        """Best-effort dismiss of a cookie/consent banner.

        Never raises.
        """
        for name in _CONSENT_BUTTON_NAMES:
            try:
                btn = self._page.get_by_role("button", name=name)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await asyncio.sleep(0.5)
            except Exception:
                continue

    async def check_session_health(self) -> bool:
        """Check if the TBF session is still valid.

        Logged in == the "Sign out" control (LOGGED_IN_MARKER) is PRESENT. This is a
        positive, fail-safe check: an unrecognized page (selector drift, a failed load)
        reads as logged-out and forces a re-login rather than letting the worker write
        anonymized "TBS Member" rows. (The original bug keyed on "TBS Member" text — the
        logged-OUT company label — and was a false positive.)
        """
        try:
            marker_count: int = await self._page.locator(LOGGED_IN_MARKER).count()
            return marker_count > 0
        except Exception as e:
            logger.warning("TBF session health check failed: {}", e)
            return False

    async def login(self) -> bool:
        """Log in to The Broker Forum with member credentials.

        Flow (verified live against the Vue SPA):
        1. Navigate to the home page (domcontentloaded + settle; networkidle
           times out on this chatty SPA).
        2. Dismiss any cookie/consent banner.
        3. Open the login modal via the "Sign In" button.
        4. Within ``form:has(input[name='password'])`` (NOT the reset form / the
           ``fakeEmail`` honeypot), fill email + password and submit.
        5. Settle, then verify via check_session_health().
        """
        if not self.config.TBF_USERNAME or not self.config.TBF_PASSWORD:
            logger.error("TBF login: TBF_USERNAME or TBF_PASSWORD not configured")
            return False

        try:
            await self._page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            await self._dismiss_consent_banner()

            # Open the login modal.
            await self._page.locator("button:has-text('Sign In')").first.click()
            await self._page.locator("input[name='password']:visible").wait_for(timeout=10000)

            # The real login form is the one that has a password field (the
            # password-RESET form has only an email field; there is also a
            # `fakeEmail` honeypot input — never fill those).
            form = self._page.locator("form:has(input[name='password'])")
            await form.locator("input[name='email']").fill(self.config.TBF_USERNAME)
            await asyncio.sleep(0.5)
            await form.locator("input[name='password']").fill(self.config.TBF_PASSWORD)
            await asyncio.sleep(0.5)
            # The form's submit control (NOT the nav "Sign In" toggle that opened
            # the modal — that one is a `type=button`).
            await form.locator("button[type=submit]").first.click()
            await asyncio.sleep(7)

            self.is_logged_in = await self.check_session_health()
            if self.is_logged_in:
                logger.info("TBF login: success")
            else:
                # If 2FA was enabled on the account, an emailed-code step blocks
                # an unattended login — surface that distinctly from bad creds.
                try:
                    if await self._page.locator("input[name='code']:visible").count() > 0:
                        logger.error(
                            "TBF login: blocked on a 2FA code step — manual re-auth required "
                            "(account now enforces email 2FA)"
                        )
                    else:
                        logger.error("TBF login: failed — still logged out after submit (check credentials)")
                except Exception:
                    logger.error("TBF login: failed — still logged out after submit")
            return self.is_logged_in

        except Exception as e:
            logger.error("TBF login: exception during login: {}", e)
            self.is_logged_in = False
            return False

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
