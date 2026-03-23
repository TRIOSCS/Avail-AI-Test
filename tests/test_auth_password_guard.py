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


class TestStartupPasswordWarning:
    """Startup should log CRITICAL when ENABLE_PASSWORD_LOGIN=true in non-test mode."""

    def test_critical_warning_when_password_login_enabled_no_testing(self):
        """ENABLE_PASSWORD_LOGIN=true + no TESTING → CRITICAL log."""
        messages = []

        def sink(message):
            messages.append(message)

        handler_id = logger.add(sink, level="CRITICAL")
        try:
            env = {
                "ENABLE_PASSWORD_LOGIN": "true",
            }
            # Remove TESTING from env temporarily
            with patch.dict(os.environ, env, clear=False):
                old_testing = os.environ.pop("TESTING", None)
                try:
                    from app.startup import run_startup_migrations

                    # The function will try DB operations after the check, so we
                    # only need to verify the log; catch the DB error.
                    try:
                        run_startup_migrations()
                    except Exception:
                        pass  # DB not available in test — that's fine

                    critical_msgs = [m for m in messages if "ENABLE_PASSWORD_LOGIN is active" in str(m)]
                    assert len(critical_msgs) >= 1, (
                        f"Expected CRITICAL log about ENABLE_PASSWORD_LOGIN, got: {messages}"
                    )
                finally:
                    if old_testing is not None:
                        os.environ["TESTING"] = old_testing
        finally:
            logger.remove(handler_id)

    def test_no_warning_when_testing_is_set(self):
        """ENABLE_PASSWORD_LOGIN=true + TESTING=1 → no CRITICAL log."""
        messages = []

        def sink(message):
            messages.append(message)

        handler_id = logger.add(sink, level="CRITICAL")
        try:
            env = {
                "ENABLE_PASSWORD_LOGIN": "true",
                "TESTING": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                from app.startup import run_startup_migrations

                try:
                    run_startup_migrations()
                except Exception:
                    pass

                critical_msgs = [m for m in messages if "ENABLE_PASSWORD_LOGIN is active" in str(m)]
                assert len(critical_msgs) == 0, f"Should NOT log CRITICAL when TESTING is set, got: {critical_msgs}"
        finally:
            logger.remove(handler_id)

    def test_no_warning_when_password_login_disabled(self):
        """ENABLE_PASSWORD_LOGIN=false → no CRITICAL log."""
        messages = []

        def sink(message):
            messages.append(message)

        handler_id = logger.add(sink, level="CRITICAL")
        try:
            env = {
                "ENABLE_PASSWORD_LOGIN": "false",
                "TESTING": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                from app.startup import run_startup_migrations

                try:
                    run_startup_migrations()
                except Exception:
                    pass

                critical_msgs = [m for m in messages if "ENABLE_PASSWORD_LOGIN is active" in str(m)]
                assert len(critical_msgs) == 0
        finally:
            logger.remove(handler_id)


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
