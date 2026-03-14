"""
tests/test_phase1_security_fixes.py — Tests for Phase 1 security quick wins.

Covers:
- secrets.compare_digest for agent API key (timing-safe)
- Rate limiting on password login endpoint
- Retry-After header capping at 300s
- Input validation on vendor_analytics query params

Called by: pytest
Depends on: app/dependencies.py, app/routers/auth.py, app/connectors/sources.py,
            app/routers/vendor_analytics.py
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.connectors.sources import _parse_retry_after


# ── Retry-After header capping ────────────────────────────────────────


class TestRetryAfterCap:
    """Verify Retry-After header is capped at 300s max."""

    def test_normal_value_unchanged(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "10"}
        assert _parse_retry_after(resp) == 10.0

    def test_minimum_is_1_second(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "0.1"}
        assert _parse_retry_after(resp) == 1.0

    def test_capped_at_300_seconds(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "999999"}
        assert _parse_retry_after(resp) == 300.0

    def test_large_value_capped(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "86400"}
        assert _parse_retry_after(resp) == 300.0

    def test_missing_header_returns_default(self):
        resp = MagicMock()
        resp.headers = {}
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0  # 5.0 + random(0, 2)

    def test_invalid_header_returns_default(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "not-a-number"}
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0


# ── Vendor analytics input validation ─────────────────────────────────


class TestVendorAnalyticsInputValidation:
    """Verify that invalid query params return 400, not 500."""

    def test_offer_history_invalid_limit(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/offer-history?limit=abc")
        assert resp.status_code == 400

    def test_offer_history_invalid_offset(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/offer-history?offset=xyz")
        assert resp.status_code == 400

    def test_confirmed_offers_invalid_limit(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/confirmed-offers?limit=abc")
        assert resp.status_code == 400

    def test_offer_history_valid_params_work(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/offer-history?limit=10&offset=0")
        assert resp.status_code == 200


# ── Agent API key uses secrets.compare_digest ──────────────────────────


class TestAgentApiKeyTimingSafe:
    """Verify agent API key comparison is timing-safe."""

    def test_agent_key_uses_compare_digest(self):
        """Ensure dependencies.py imports and uses secrets.compare_digest."""
        import inspect

        from app import dependencies

        source = inspect.getsource(dependencies.require_user)
        assert "secrets.compare_digest" in source
        assert 'agent_key == settings.agent_api_key' not in source


# ── Password login rate limiting ───────────────────────────────────────


class TestPasswordLoginRateLimit:
    """Verify password login endpoint has rate limiting decorator."""

    def test_password_login_has_limiter(self):
        """Ensure the password_login function has rate limiting applied."""
        import inspect

        from app.routers import auth

        source = inspect.getsource(auth)
        # Find the password_login function and check it has limiter
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "async def password_login" in line:
                # Check preceding lines for limiter decorator
                context = "\n".join(lines[max(0, i - 3) : i + 1])
                assert "limiter.limit" in context, "password_login missing rate limit decorator"
                return
        pytest.fail("password_login function not found")
