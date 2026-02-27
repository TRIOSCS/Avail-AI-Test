"""Tests for NC Phase 4: Human Behavior + Browser Session Manager.

Called by: pytest
Depends on: conftest.py, nc_worker.human_behavior, nc_worker.session_manager
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.nc_worker.config import NcConfig
from app.services.nc_worker.human_behavior import HumanBehavior
from app.services.nc_worker.session_manager import NcSessionManager


# ── HumanBehavior Tests ─────────────────────────────────────────────


def test_random_delay_within_bounds():
    """random_delay stays within min/max bounds across 100 iterations."""
    loop = asyncio.new_event_loop()
    for _ in range(100):
        start = time.monotonic()
        loop.run_until_complete(HumanBehavior.random_delay(0.01, 0.03))
        elapsed = time.monotonic() - start
        assert elapsed >= 0.009  # Small tolerance for scheduling
        assert elapsed < 0.1  # Should never exceed max by much
    loop.close()


def test_random_delay_gaussian_distribution():
    """Delays cluster around the middle of the range."""
    loop = asyncio.new_event_loop()
    delays = []
    for _ in range(50):
        start = time.monotonic()
        loop.run_until_complete(HumanBehavior.random_delay(0.01, 0.05))
        delays.append(time.monotonic() - start)
    loop.close()
    avg = sum(delays) / len(delays)
    # Average should be roughly in the middle (0.03), not at extremes
    assert 0.015 < avg < 0.055


def test_human_type_calls_keyboard(db_session):
    """human_type types each character individually."""
    loop = asyncio.new_event_loop()
    page = MagicMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock()
    locator = MagicMock()
    locator.click = AsyncMock()

    loop.run_until_complete(HumanBehavior.human_type(page, locator, "ABC"))
    loop.close()

    locator.click.assert_called_once()
    assert page.keyboard.type.call_count == 3  # One per character


def test_human_click_random_position():
    """human_click clicks within the bounding box, not dead center."""
    loop = asyncio.new_event_loop()
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    locator = MagicMock()
    locator.bounding_box = AsyncMock(return_value={
        "x": 100, "y": 200, "width": 80, "height": 30
    })

    loop.run_until_complete(HumanBehavior.human_click(page, locator))
    loop.close()

    page.mouse.click.assert_called_once()
    call_args = page.mouse.click.call_args[0]
    x, y = call_args
    # Should be within the box (30-70% range)
    assert 100 + 80 * 0.3 <= x <= 100 + 80 * 0.7
    assert 200 + 30 * 0.3 <= y <= 200 + 30 * 0.7


def test_human_click_fallback_no_bbox():
    """human_click falls back to locator.click() if bounding_box returns None."""
    loop = asyncio.new_event_loop()
    page = MagicMock()
    locator = MagicMock()
    locator.bounding_box = AsyncMock(return_value=None)
    locator.click = AsyncMock()

    loop.run_until_complete(HumanBehavior.human_click(page, locator))
    loop.close()

    locator.click.assert_called_once()


# ── NcSessionManager Tests ──────────────────────────────────────────


def test_session_manager_init():
    """NcSessionManager can be instantiated with config."""
    cfg = NcConfig()
    mgr = NcSessionManager(cfg)
    assert mgr.config is cfg
    assert mgr.is_logged_in is False
    assert mgr.page is None


def test_session_manager_login_no_credentials():
    """Login fails gracefully when credentials are missing."""
    cfg = NcConfig()
    cfg.NC_USERNAME = ""
    cfg.NC_PASSWORD = ""
    mgr = NcSessionManager(cfg)
    mgr._page = MagicMock()

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(mgr.login())
    loop.close()

    assert result is False
    assert mgr.is_logged_in is False


def test_session_manager_ensure_session_healthy():
    """ensure_session returns True if session is already healthy."""
    cfg = NcConfig()
    mgr = NcSessionManager(cfg)
    mgr._page = MagicMock()
    mgr._page.evaluate = AsyncMock(return_value={"status": 200, "body": "true"})

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(mgr.ensure_session())
    loop.close()

    assert result is True
    assert mgr.is_logged_in is True


def test_session_manager_stop():
    """stop() cleans up state."""
    cfg = NcConfig()
    mgr = NcSessionManager(cfg)
    mgr._context = MagicMock()
    mgr._context.close = AsyncMock()
    mgr._playwright = MagicMock()
    mgr._playwright.stop = AsyncMock()
    mgr.is_logged_in = True

    loop = asyncio.new_event_loop()
    loop.run_until_complete(mgr.stop())
    loop.close()

    assert mgr._context is None
    assert mgr._page is None
    assert mgr._playwright is None
    assert mgr.is_logged_in is False
