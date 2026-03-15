"""NetComponents session manager — hybrid HTTP + browser.

Manages authentication for both HTTP (requests.Session) and browser
(Patchright) approaches. The HTTP session is always maintained for
fast operations. The browser is only started if HTTP search fails.

Called by: worker loop
Depends on: requests, beautifulsoup4, patchright (optional), config
"""

import os

import requests
from bs4 import BeautifulSoup
from loguru import logger

from .config import NcConfig

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class NcSessionManager:
    """Manages the NetComponents session lifecycle (HTTP + optional browser)."""

    def __init__(self, config: NcConfig):
        self.config = config
        # HTTP session (always available)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        # Browser (lazy-started only when needed)
        self._playwright = None
        self._context = None
        self._page = None
        self._browser_started = False
        self.is_logged_in = False

    @property
    def page(self):
        """Browser page (only available after start_browser)."""
        return self._page

    @property
    def has_browser(self) -> bool:
        return self._browser_started and self._page is not None

    def start(self):
        """Initialize HTTP session by loading the homepage."""
        try:
            resp = self.session.get("https://www.netcomponents.com/", timeout=30)
            resp.raise_for_status()
            self.is_logged_in = self.check_session_health()
            if self.is_logged_in:
                logger.info("NC session: already logged in (HTTP cookies)")
            else:
                logger.info("NC session: not logged in, will need to authenticate")
        except Exception as e:
            logger.error("NC session: failed to load homepage: {}", e)
            raise

    async def start_browser(self):
        """Start the Patchright browser for JS-based search (fallback mode)."""
        if self._browser_started:
            return

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
        self._browser_started = True
        logger.info("NC session: browser started (fallback mode)")

    def check_session_health(self) -> bool:
        """Check if the NC session is still valid via /client/isauthorized."""
        try:
            resp = self.session.get(
                "https://www.netcomponents.com/client/isauthorized",
                timeout=15,
            )
            is_auth = resp.status_code == 200 and "true" in resp.text.lower()
            return is_auth
        except Exception as e:
            logger.warning("NC session health check failed: {}", e)
            return False

    def login(self) -> bool:
        """Log in to NetComponents via HTTP POST.

        NC login form has three fields: Account #, Login Name, Password.
        Uses X-Requested-With header to get the form HTML (not SPA redirect).
        """
        if not self.config.NC_ACCOUNT_NUMBER or not self.config.NC_USERNAME or not self.config.NC_PASSWORD:
            logger.error("NC login: NC_ACCOUNT_NUMBER, NC_USERNAME, or NC_PASSWORD not configured")
            return False

        try:
            # Load login form via XHR to get CSRF token
            login_page = self.session.get(
                "https://www.netcomponents.com/account/login",
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=30,
            )
            login_page.raise_for_status()

            soup = BeautifulSoup(login_page.text, "html.parser")
            token_input = soup.find("input", {"name": "__RequestVerificationToken"})
            if not token_input:
                logger.error("NC login: could not find __RequestVerificationToken")
                return False
            token = token_input["value"]

            # POST login
            resp = self.session.post(
                "https://www.netcomponents.com/account/login",
                data={
                    "__RequestVerificationToken": token,
                    "AccountNumber": self.config.NC_ACCOUNT_NUMBER,
                    "UserName": self.config.NC_USERNAME,
                    "Password": self.config.NC_PASSWORD,
                    "RememberMe": "false",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=30,
                allow_redirects=True,
            )

            self.is_logged_in = self.check_session_health()
            if self.is_logged_in:
                logger.info("NC login: success (HTTP)")
            else:
                logger.error("NC login: failed (status={})", resp.status_code)
            return self.is_logged_in

        except Exception as e:
            logger.error("NC login: exception: {}", e)
            self.is_logged_in = False
            return False

    async def login_browser(self) -> bool:
        """Log in via browser (fallback if HTTP login fails)."""
        from .human_behavior import HumanBehavior

        if not self.has_browser:
            await self.start_browser()

        try:
            await self._page.goto("https://www.netcomponents.com/#/account/login")
            import asyncio

            await asyncio.sleep(3)

            acct_input = self._page.locator("#AccountNumber")
            await acct_input.wait_for(timeout=15000)
            await acct_input.fill("")
            await HumanBehavior.human_type(self._page, acct_input, self.config.NC_ACCOUNT_NUMBER)
            await HumanBehavior.random_delay(0.3, 0.8)

            user_input = self._page.locator("#UserName")
            await user_input.fill("")
            await HumanBehavior.human_type(self._page, user_input, self.config.NC_USERNAME)
            await HumanBehavior.random_delay(0.3, 0.8)

            pwd_input = self._page.locator("#Password")
            await pwd_input.fill("")
            await HumanBehavior.human_type(self._page, pwd_input, self.config.NC_PASSWORD)
            await HumanBehavior.random_delay(0.5, 1.0)

            login_btn = self._page.locator("input.login-button")
            await login_btn.click()
            await asyncio.sleep(5)

            # Check auth via browser
            result = await self._page.evaluate("""
                async () => {
                    const r = await fetch('/client/isauthorized', {credentials: 'same-origin'});
                    return {status: r.status, body: await r.text()};
                }
            """)
            self.is_logged_in = result.get("status") == 200 and "true" in result.get("body", "").lower()
            if self.is_logged_in:
                logger.info("NC login: success (browser)")
            else:
                logger.error("NC login: failed (browser)")
            return self.is_logged_in

        except Exception as e:
            logger.error("NC login (browser): exception: {}", e)
            self.is_logged_in = False
            return False

    def ensure_session(self) -> bool:
        """Ensure we have a valid session, re-logging in if needed."""
        if self.check_session_health():
            self.is_logged_in = True
            return True
        logger.info("NC session expired, re-authenticating...")
        return self.login()

    def stop(self):
        """Close HTTP session and browser if running."""
        try:
            self.session.close()
        except Exception:
            pass
        self.is_logged_in = False

    async def stop_browser(self):
        """Close browser context and stop Playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("NC session browser stop error: {}", e)
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            self._browser_started = False
