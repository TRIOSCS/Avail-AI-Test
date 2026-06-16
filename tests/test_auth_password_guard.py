"""test_auth_password_guard.py — Tests for password login production guard.

Verifies:
1. Startup logs a CRITICAL warning when ENABLE_PASSWORD_LOGIN=true without TESTING set
2. /auth/login-form has rate limiting applied (5/minute)

Called by: pytest
Depends on: app.startup, app.routers.auth
"""

import os
from unittest.mock import patch

from loguru import logger


def _critical_msgs_from_startup(env: dict[str, str], *, remove_keys: tuple[str, ...] = ()) -> list[str]:
    """Run startup under ``env`` (with ``remove_keys`` popped from os.environ) and
    return the captured CRITICAL log lines mentioning ENABLE_PASSWORD_LOGIN.

    Startup attempts DB operations after the guard check, so the DB error is swallowed —
    only the log is under test.
    """
    messages = []
    handler_id = logger.add(messages.append, level="CRITICAL")
    saved = {}
    try:
        with patch.dict(os.environ, env, clear=False):
            for key in remove_keys:
                saved[key] = os.environ.pop(key, None)
            try:
                from app.startup import run_startup_migrations

                try:
                    run_startup_migrations()
                except Exception:
                    pass  # DB not available in test — that's fine
            finally:
                for key, value in saved.items():
                    if value is not None:
                        os.environ[key] = value
        return [m for m in messages if "ENABLE_PASSWORD_LOGIN is active" in str(m)]
    finally:
        logger.remove(handler_id)


class TestStartupPasswordWarning:
    """Startup should log CRITICAL when ENABLE_PASSWORD_LOGIN=true in non-test mode."""

    def test_critical_warning_when_password_login_enabled_no_testing(self):
        """ENABLE_PASSWORD_LOGIN=true + no TESTING → CRITICAL log."""
        critical_msgs = _critical_msgs_from_startup({"ENABLE_PASSWORD_LOGIN": "true"}, remove_keys=("TESTING",))
        assert len(critical_msgs) >= 1, f"Expected CRITICAL log about ENABLE_PASSWORD_LOGIN, got: {critical_msgs}"

    def test_no_warning_when_testing_is_set(self):
        """ENABLE_PASSWORD_LOGIN=true + TESTING=1 → no CRITICAL log."""
        critical_msgs = _critical_msgs_from_startup({"ENABLE_PASSWORD_LOGIN": "true", "TESTING": "1"})
        assert len(critical_msgs) == 0, f"Should NOT log CRITICAL when TESTING is set, got: {critical_msgs}"

    def test_no_warning_when_password_login_disabled(self):
        """ENABLE_PASSWORD_LOGIN=false → no CRITICAL log."""
        critical_msgs = _critical_msgs_from_startup({"ENABLE_PASSWORD_LOGIN": "false", "TESTING": "1"})
        assert len(critical_msgs) == 0


class TestLoginFormRateLimit:
    """Verify /auth/login-form has rate limiting applied."""

    def test_login_form_has_rate_limit_decorator(self):
        """The login-form endpoint should have a rate limit of 5/minute."""
        # Import the router module to trigger decorator registration
        import app.routers.auth  # noqa: F401
        from app.rate_limit import limiter

        # SlowAPI stores limits in _route_limits as Dict[str, List[Limit]]
        # keyed by the fully qualified function name.
        matching_keys = [k for k in limiter._route_limits if "password_login_form" in k]
        assert matching_keys, (
            f"password_login_form should have rate limiting. Registered endpoints: {list(limiter._route_limits.keys())}"
        )
        # Verify the limit is 5/minute
        for key in matching_keys:
            limits = limiter._route_limits[key]
            limit_strs = [str(lim.limit) for lim in limits]
            assert any("5" in s and "minute" in s for s in limit_strs), (
                f"Expected 5/minute rate limit on {key}, got: {limit_strs}"
            )

    def test_login_post_has_rate_limit_decorator(self):
        """The POST /auth/login endpoint should also have rate limiting."""
        import app.routers.auth  # noqa: F401
        from app.rate_limit import limiter

        matching_keys = [k for k in limiter._route_limits if k.endswith("password_login")]
        assert matching_keys, (
            f"password_login (POST) should have rate limiting. "
            f"Registered endpoints: {list(limiter._route_limits.keys())}"
        )
