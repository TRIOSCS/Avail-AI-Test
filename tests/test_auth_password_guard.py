"""test_auth_password_guard.py — Tests for password login production guard.

Verifies:
1. A real (non-TESTING) boot RAISES when ENABLE_PASSWORD_LOGIN=true unless
   ALLOW_PASSWORD_LOGIN_RISK=true acknowledges the auth-bypass risk
2. The acknowledged (ack) boot logs a CRITICAL warning instead of raising
3. /auth/login-form has rate limiting applied (5/minute)

Called by: pytest
Depends on: app.startup, app.routers.auth
"""

import os
from unittest.mock import patch

import pytest
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
    """Startup logs CRITICAL when ENABLE_PASSWORD_LOGIN=true is acknowledged.

    The unacknowledged non-test path no longer logs-and-continues — it fail-boots (see
    TestStartupPasswordFailBoot). The CRITICAL log now marks only the explicitly-
    acknowledged (ALLOW_PASSWORD_LOGIN_RISK=true) boot.
    """

    def test_critical_warning_when_password_login_enabled_with_ack(self):
        """ENABLE_PASSWORD_LOGIN=true + ALLOW_PASSWORD_LOGIN_RISK=true + no TESTING →
        CRITICAL log."""
        critical_msgs = _critical_msgs_from_startup(
            {"ENABLE_PASSWORD_LOGIN": "true", "ALLOW_PASSWORD_LOGIN_RISK": "true"},
            remove_keys=("TESTING",),
        )
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


class TestStartupPasswordFailBoot:
    """Real (non-TESTING) boot must RAISE when ENABLE_PASSWORD_LOGIN=true unless
    ALLOW_PASSWORD_LOGIN_RISK=true acknowledges the auth-bypass risk.

    The log-only CRITICAL warning (TestStartupPasswordWarning) was upgraded to a fail-
    boot guard so the password-login bypass can never silently reach an environment
    where the operator has not explicitly acknowledged the risk.
    """

    def test_boot_raises_without_ack(self):
        """ENABLE_PASSWORD_LOGIN=true, no TESTING, no ack → RuntimeError at boot.

        The guard raises BEFORE any DB access, so no DB patching is needed here.
        """
        from app.startup import run_startup_migrations

        with patch.dict(os.environ, {"ENABLE_PASSWORD_LOGIN": "true"}, clear=False):
            os.environ.pop("TESTING", None)
            os.environ.pop("ALLOW_PASSWORD_LOGIN_RISK", None)
            try:
                with pytest.raises(RuntimeError, match="ALLOW_PASSWORD_LOGIN_RISK"):
                    run_startup_migrations()
            finally:
                os.environ["TESTING"] = "1"

    def test_boot_succeeds_with_ack(self):
        """ENABLE_PASSWORD_LOGIN=true + ALLOW_PASSWORD_LOGIN_RISK=true → boots.

        Every conn-taking FAST helper and the DB-touching seed helpers are stubbed
        (engine patched to in-memory SQLite) so the guard is the only behavior under
        test — it must NOT raise when the risk is acknowledged.
        """
        from app.startup import run_startup_migrations
        from tests.test_startup import _make_sqlite_engine

        env = {"ENABLE_PASSWORD_LOGIN": "true", "ALLOW_PASSWORD_LOGIN_RISK": "true"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TESTING", None)
            try:
                with (
                    patch("app.startup.engine", _make_sqlite_engine()),
                    patch("app.startup._create_fts_triggers"),
                    patch("app.startup._seed_system_config"),
                    patch("app.startup._reconcile_system_config"),
                    patch("app.startup._seed_manufacturers"),
                    patch("app.startup._create_count_triggers"),
                    patch("app.startup._reconcile_connector_active"),
                    patch("app.startup._verify_encryption_canary"),
                    patch("app.startup._create_default_user_if_env_set"),
                    patch("app.startup._seed_admin_user_if_env_set"),
                    patch("app.startup._seed_agent_user"),
                    patch("app.startup._seed_verification_group_from_admin_emails"),
                    patch("app.startup._seed_commodity_schemas"),
                ):
                    run_startup_migrations()  # must NOT raise
            finally:
                os.environ["TESTING"] = "1"
