"""tests/test_rate_limiting.py — Tests for rate limiting behavior.

Covers: slowapi rate limiter configuration, key function, storage
backend resolution, TESTING-mode fallback behavior, AND actual
enforcement — both the per-(user, bucket) ``check_rate_limit`` counter
(block on limit+1, reset on window rollover via the ``_now`` seam) and
the slowapi HTTP throttle used on the public pay-link endpoints, driven
through an isolated ENABLED ``Limiter`` (the global one is disabled by
``RATE_LIMIT_ENABLED=false`` under TESTING).

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


# ── Enforcement: per-(user, bucket) check_rate_limit counter ──────────────
# These drive the actual counter (not just config). The Redis substrate is
# patched to None so the deterministic in-memory fallback is exercised, and
# the fixed-window clock (``_now``) is frozen/advanced explicitly.


def test_check_rate_limit_blocks_the_call_after_the_limit():
    """Allows exactly ``limit`` calls in a window, blocks the (limit+1)th."""
    from app import rate_limit
    from app.rate_limit import check_rate_limit, reset_rate_limit_state

    limit = 3
    with (
        patch.object(rate_limit, "_get_redis", return_value=None),
        patch.object(rate_limit, "_now", lambda: 1_000_000.0),
    ):
        reset_rate_limit_state()
        verdicts = [
            check_rate_limit(user_id=7, bucket="call", limit=limit, window_seconds=60) for _ in range(limit + 1)
        ]
        reset_rate_limit_state()

    # First ``limit`` calls allowed (True); the extra call blocked (False).
    assert verdicts == [True, True, True, False]


def test_check_rate_limit_resets_on_window_rollover():
    """Exhausting a window blocks; advancing ``_now`` one full window re-allows."""
    from app import rate_limit
    from app.rate_limit import check_rate_limit, reset_rate_limit_state

    limit = 2
    window = 60

    with patch.object(rate_limit, "_get_redis", return_value=None):
        reset_rate_limit_state()
        # Window A: use up the budget, then confirm the next call is blocked.
        with patch.object(rate_limit, "_now", lambda: 5_000_000.0):
            for _ in range(limit):
                assert check_rate_limit(9, "email", limit=limit, window_seconds=window) is True
            blocked = check_rate_limit(9, "email", limit=limit, window_seconds=window)

        # Window B: same user/bucket, clock advanced one full window → allowed again.
        with patch.object(rate_limit, "_now", lambda: 5_000_000.0 + window):
            after_rollover = check_rate_limit(9, "email", limit=limit, window_seconds=window)
        reset_rate_limit_state()

    assert blocked is False
    assert after_rollover is True


# ── Enforcement: slowapi HTTP throttle (pay-link endpoints) ───────────────
# The public prepayment pay-link routes carry ``@limiter.limit("10/minute")``.
# The GLOBAL limiter is disabled under TESTING (``RATE_LIMIT_ENABLED=false``),
# so config alone never proves the throttle fires. Build an isolated ENABLED
# limiter wired exactly like app/main.py and drive it over the limit.


def _isolated_limited_client(limit_str: str):
    """A tiny FastAPI app with its OWN enabled in-memory Limiter, wired like app/main.py
    (exception handler + SlowAPIMiddleware), exposing one route throttled at
    ``limit_str`` — mirrors the pay-link ``@limiter.limit`` guard."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address, enabled=True, storage_uri="memory://")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/p/confirm/{token}")
    @limiter.limit(limit_str)
    async def _confirm(request: Request, token: str):
        return {"ok": True}

    return TestClient(app)


def test_slowapi_limiter_blocks_request_over_the_limit():
    """An ENABLED slowapi limiter allows ``limit`` requests, then returns 429."""
    limit = 3
    client = _isolated_limited_client(f"{limit}/minute")

    codes = [client.get("/p/confirm/tok").status_code for _ in range(limit + 1)]

    # First ``limit`` requests pass (200); the (limit+1)th is throttled (429).
    assert codes[:limit] == [200] * limit
    assert codes[limit] == 429
