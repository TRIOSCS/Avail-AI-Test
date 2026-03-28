"""Tests for app/services/ics_worker/session_manager.py — browser session lifecycle.

Called by: pytest
Depends on: conftest fixtures, unittest.mock (all browser/playwright calls mocked)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ics_worker.config import IcsConfig
from app.services.ics_worker.session_manager import IcsSessionManager


@pytest.fixture()
def config():
    """ICS config with test credentials."""
    c = IcsConfig()
    c.ICS_USERNAME = "testuser"
    c.ICS_PASSWORD = "testpass"
    c.ICS_BROWSER_PROFILE_DIR = "/tmp/test_ics_profile"
    return c


@pytest.fixture()
def manager(config):
    """Fresh session manager instance."""
    return IcsSessionManager(config)


@pytest.fixture()
def mock_page():
    """A mock Playwright page object."""
    page = AsyncMock()
    page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"
    page.goto = AsyncMock()
    page.locator = MagicMock()
    page.evaluate = AsyncMock()
    page.keyboard = AsyncMock()
    page.mouse = AsyncMock()
    return page


@pytest.fixture()
def mock_context(mock_page):
    """A mock Playwright browser context."""
    ctx = AsyncMock()
    ctx.pages = [mock_page]
    ctx.close = AsyncMock()
    return ctx


@pytest.fixture()
def mock_playwright(mock_context):
    """A mock Playwright instance."""
    pw = AsyncMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
    pw.stop = AsyncMock()
    return pw


# ── __init__ / properties ───────────────────────────────────────────


class TestInit:
    def test_initial_state(self, manager):
        assert manager.is_logged_in is False
        assert manager.page is None
        assert manager._playwright is None
        assert manager._context is None

    def test_page_property(self, manager, mock_page):
        manager._page = mock_page
        assert manager.page is mock_page


# ── start ───────────────────────────────────────────────────────────


class TestStart:
    def test_start_no_display_raises(self, manager):
        """Start() raises RuntimeError when DISPLAY is not set."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove DISPLAY if present
            with patch.dict("os.environ", {"DISPLAY": ""}, clear=False):
                # Actually need to remove DISPLAY
                import os

                old = os.environ.pop("DISPLAY", None)
                try:
                    with pytest.raises(RuntimeError, match="DISPLAY environment variable not set"):
                        asyncio.get_event_loop().run_until_complete(manager.start())
                finally:
                    if old is not None:
                        os.environ["DISPLAY"] = old

    def test_start_success_logged_in(self, manager, mock_playwright, mock_page):
        """Start() launches browser and detects existing login."""
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch.dict("os.environ", {"DISPLAY": ":99"}):
                with patch(
                    "app.services.ics_worker.session_manager.async_playwright",
                    return_value=AsyncMock(
                        start=AsyncMock(return_value=mock_playwright),
                    ),
                ):
                    # Mock async_playwright properly
                    mock_pw_cm = AsyncMock()
                    mock_pw_cm.start = AsyncMock(return_value=mock_playwright)

                    with patch("app.services.ics_worker.session_manager.async_playwright") as mock_apw:
                        mock_apw.return_value.start = AsyncMock(return_value=mock_playwright)
                        await manager.start()

        # Since start() imports patchright, we need to mock the import
        with patch.dict("os.environ", {"DISPLAY": ":99"}):
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                mock_pw_factory = AsyncMock()
                mock_pw_factory.start = AsyncMock(return_value=mock_playwright)

                with patch(
                    "patchright.async_api.async_playwright",
                    return_value=mock_pw_factory,
                ):
                    asyncio.get_event_loop().run_until_complete(manager.start())

        assert manager._page is mock_page
        assert manager.is_logged_in is True

    def test_start_not_logged_in(self, manager, mock_playwright, mock_page):
        """Start() detects when not logged in (redirect to login page)."""
        mock_page.url = "https://www.icsource.com/home/Login.aspx"

        with patch.dict("os.environ", {"DISPLAY": ":99"}):
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                mock_pw_factory = AsyncMock()
                mock_pw_factory.start = AsyncMock(return_value=mock_playwright)

                with patch(
                    "patchright.async_api.async_playwright",
                    return_value=mock_pw_factory,
                ):
                    asyncio.get_event_loop().run_until_complete(manager.start())

        assert manager.is_logged_in is False


# ── check_session_health ────────────────────────────────────────────


class TestCheckSessionHealth:
    def test_healthy_session(self, manager, mock_page):
        manager._page = mock_page
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                return await manager.check_session_health()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True

    def test_redirected_to_login(self, manager, mock_page):
        manager._page = mock_page
        mock_page.url = "https://www.icsource.com/home/Login.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                return await manager.check_session_health()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_redirected_to_public_home(self, manager, mock_page):
        manager._page = mock_page
        mock_page.url = "https://www.icsource.com/home/default.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                return await manager.check_session_health()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_exception_returns_false(self, manager, mock_page):
        manager._page = mock_page
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))

        async def _run():
            return await manager.check_session_health()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False


# ── login ───────────────────────────────────────────────────────────


class TestLogin:
    def test_login_no_credentials(self, manager, mock_page):
        """Login() fails gracefully when credentials are missing."""
        manager.config.ICS_USERNAME = ""
        manager.config.ICS_PASSWORD = ""
        manager._page = mock_page

        async def _run():
            return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False
        assert manager.is_logged_in is False

    def test_login_success(self, manager, mock_page):
        """Login() follows the full login flow and verifies session."""
        manager._page = mock_page

        # Mock the locator chain
        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()
        mock_page.locator = MagicMock(return_value=username_locator)

        # After login, health check returns success
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True
        assert manager.is_logged_in is True

    def test_login_fails_session_check(self, manager, mock_page):
        """Login() returns False when post-login health check fails."""
        manager._page = mock_page

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()
        mock_page.locator = MagicMock(return_value=username_locator)

        # After login, redirected to login page (failed)
        mock_page.url = "https://www.icsource.com/home/Login.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False
        assert manager.is_logged_in is False

    def test_login_exception(self, manager, mock_page):
        """Login() returns False on unexpected exception."""
        manager._page = mock_page
        mock_page.goto = AsyncMock(side_effect=Exception("Browser crashed"))

        async def _run():
            return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False
        assert manager.is_logged_in is False

    def test_login_password_placeholder_missing(self, manager, mock_page):
        """Login() still works when password placeholder is absent."""
        manager._page = mock_page

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()

        pwd_locator = AsyncMock()
        pwd_locator.wait_for = AsyncMock(side_effect=Exception("Timeout"))

        login_btn = AsyncMock()
        login_btn.wait_for = AsyncMock()

        def locator_side_effect(selector):
            if "passwordhidden" in selector:
                return pwd_locator
            if "button.green" in selector:
                return login_btn
            return username_locator

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True

    def test_login_green_button_not_found_fallback(self, manager, mock_page):
        """Login() falls back to ASP.NET button if green div not found."""
        manager._page = mock_page

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()

        pwd_locator = AsyncMock()
        pwd_locator.wait_for = AsyncMock()
        pwd_locator.click = AsyncMock()

        call_count = 0
        fallback_btn = AsyncMock()
        fallback_btn.wait_for = AsyncMock()
        green_btn = AsyncMock()
        green_btn.wait_for = AsyncMock(side_effect=Exception("Not visible"))

        def locator_side_effect(selector):
            if "passwordhidden" in selector:
                return pwd_locator
            if "button.green" in selector:
                return green_btn
            if "btnLogIn" in selector:
                return fallback_btn
            return username_locator

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.login()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True


# ── ensure_session ──────────────────────────────────────────────────


class TestEnsureSession:
    def test_session_already_healthy(self, manager, mock_page):
        manager._page = mock_page
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                return await manager.ensure_session()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True
        assert manager.is_logged_in is True

    def test_session_expired_relogin_success(self, manager, mock_page):
        manager._page = mock_page

        health_call_count = 0

        async def dynamic_health(*args, **kwargs):
            nonlocal health_call_count
            health_call_count += 1
            if health_call_count == 1:
                # First call (from ensure_session): expired
                mock_page.url = "https://www.icsource.com/home/Login.aspx"
            else:
                # After login: success
                mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        mock_page.goto = dynamic_health

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()
        mock_page.locator = MagicMock(return_value=username_locator)

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.ensure_session()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True

    def test_session_expired_relogin_fails(self, manager, mock_page):
        manager._page = mock_page
        mock_page.url = "https://www.icsource.com/home/Login.aspx"

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()
        mock_page.locator = MagicMock(return_value=username_locator)

        async def _run():
            with patch("app.services.ics_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "app.services.ics_worker.session_manager.HumanBehavior.random_delay",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "app.services.ics_worker.session_manager.HumanBehavior.human_click",
                        new_callable=AsyncMock,
                    ):
                        return await manager.ensure_session()

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False


# ── stop ────────────────────────────────────────────────────────────


class TestStop:
    def test_stop_clean(self, manager, mock_playwright, mock_context, mock_page):
        manager._playwright = mock_playwright
        manager._context = mock_context
        manager._page = mock_page
        manager.is_logged_in = True

        asyncio.get_event_loop().run_until_complete(manager.stop())

        mock_context.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()
        assert manager._context is None
        assert manager._page is None
        assert manager._playwright is None
        assert manager.is_logged_in is False

    def test_stop_with_exception(self, manager, mock_context):
        manager._context = mock_context
        manager._playwright = None
        mock_context.close = AsyncMock(side_effect=Exception("Close failed"))

        asyncio.get_event_loop().run_until_complete(manager.stop())

        # Should still clean up state despite exception
        assert manager._context is None
        assert manager._page is None
        assert manager.is_logged_in is False

    def test_stop_when_not_started(self, manager):
        """Stop() on fresh manager should not raise."""
        asyncio.get_event_loop().run_until_complete(manager.stop())
        assert manager.is_logged_in is False
