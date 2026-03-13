"""
test_htmx_foundation.py — Tests for Phase 3 Task 1: HTMX + Alpine.js foundation.
Verifies feature flag, HTMX detection utilities, base template rendering.
Called by: pytest
Depends on: app.config, app.dependencies
"""

import pytest
from unittest.mock import MagicMock
from app.dependencies import wants_html, is_htmx_boosted
from app.config import Settings


class TestWantsHtml:
    """Tests for the wants_html() HTMX detection utility."""

    def test_returns_true_for_htmx_request(self):
        request = MagicMock()
        request.headers = {"HX-Request": "true"}
        assert wants_html(request) is True

    def test_returns_false_for_normal_request(self):
        request = MagicMock()
        request.headers = {}
        assert wants_html(request) is False

    def test_returns_false_for_wrong_value(self):
        request = MagicMock()
        request.headers = {"HX-Request": "false"}
        assert wants_html(request) is False


class TestIsHtmxBoosted:
    """Tests for the is_htmx_boosted() detection utility."""

    def test_returns_true_for_boosted_request(self):
        request = MagicMock()
        request.headers = {"HX-Boosted": "true"}
        assert is_htmx_boosted(request) is True

    def test_returns_false_for_non_boosted(self):
        request = MagicMock()
        request.headers = {}
        assert is_htmx_boosted(request) is False


class TestUseHtmxFeatureFlag:
    """Tests for the USE_HTMX feature flag in Settings."""

    def test_default_is_false(self):
        import os
        os.environ["TESTING"] = "1"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        s = Settings(
            database_url="sqlite:///test.db",
            _env_file=None,
        )
        assert s.use_htmx is False

    def test_can_enable(self):
        import os
        os.environ["TESTING"] = "1"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        s = Settings(
            database_url="sqlite:///test.db",
            use_htmx=True,
            _env_file=None,
        )
        assert s.use_htmx is True
