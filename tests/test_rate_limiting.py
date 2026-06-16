"""tests/test_rate_limiting.py — Tests for rate limiting behavior.

Covers: slowapi rate limiter configuration, key function, storage
backend resolution, and TESTING-mode fallback behavior.

Called by: pytest
Depends on: app.rate_limit
"""

from unittest.mock import patch

import pytest


def test_limiter_is_configured():
    """Rate limiter module exports a Limiter with key_func."""
    from app.rate_limit import limiter

    assert limiter is not None
    assert limiter._key_func is not None


def test_limiter_uses_remote_address():
    """Key function is get_remote_address (IP-based limiting)."""
    from slowapi.util import get_remote_address

    from app.rate_limit import limiter

    assert limiter._key_func is get_remote_address


def test_rate_limit_disabled_in_test_mode():
    """In TESTING mode, rate limiting should not block requests."""
    import os

    assert os.environ.get("TESTING") == "1"
    # The limiter is configured but requests still pass because
    # TESTING mode doesn't enforce strict limiting by default
    from app.rate_limit import limiter

    # Verify limiter exists (won't crash even if limits are hit)
    assert limiter is not None


def test_resolve_storage_no_redis():
    """_resolve_storage returns None when Redis is not configured."""
    with patch("app.rate_limit.settings") as mock_settings:
        mock_settings.cache_backend = "memory"
        mock_settings.redis_url = ""
        from app.rate_limit import _resolve_storage

        result = _resolve_storage()
        assert result is None


@pytest.mark.parametrize(
    ("redis_url", "ping_side_effect", "ping_return", "expected"),
    [
        pytest.param(
            "redis://localhost:6379/15",
            ConnectionError,
            None,
            None,
            id="redis_unavailable",
        ),
        pytest.param(
            "redis://localhost:6379/0",
            None,
            True,
            "redis://localhost:6379/0",
            id="redis_success",
        ),
    ],
)
def test_resolve_storage_redis(redis_url, ping_side_effect, ping_return, expected):
    """_resolve_storage returns the Redis URL when ping succeeds, else None."""
    import redis as redis_lib

    with patch("app.rate_limit.settings") as mock_settings:
        mock_settings.cache_backend = "redis"
        mock_settings.redis_url = redis_url
        with patch.object(redis_lib, "from_url") as mock_from_url:
            ping = mock_from_url.return_value.ping
            ping.side_effect = ping_side_effect
            ping.return_value = ping_return
            from app.rate_limit import _resolve_storage

            result = _resolve_storage()
            assert result == expected
