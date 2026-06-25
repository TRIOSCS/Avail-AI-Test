"""
Purpose: Tests for app/config.py pydantic-settings migration.
Description: Validates typed env var parsing, default values, validators,
    and CSV-to-list coercion for comma-separated fields.
Called by: pytest
Depends on: app.config.Settings
"""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings


def _make(**overrides) -> Settings:
    """Create a Settings instance ignoring the .env file (pure env-var test)."""
    return Settings(_env_file=None, **overrides)


def _settings_with(env: dict[str, str]) -> Settings:
    """Build a Settings from a clean environment containing only ``env``."""
    with patch.dict(os.environ, env, clear=True):
        return _make()


class TestDefaults:
    """Default values apply when env vars are absent."""

    @pytest.mark.parametrize(
        ("attr", "expected"),
        [
            ("app_url", "http://localhost:8000"),
            ("database_url", "postgresql://availai:availai@db:5432/availai"),
            ("rate_limit_enabled", True),
            ("admin_emails", []),
            ("own_domains", frozenset({"trioscs.com"})),
        ],
    )
    def test_default(self, attr, expected):
        s = _settings_with({})
        assert getattr(s, attr) == expected


class TestBooleanCoercion:
    """Boolean env vars coerce string values correctly."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("true", True),
            ("TRUE", True),
            ("false", False),
            ("1", True),
            ("0", False),
        ],
    )
    def test_rate_limit_enabled(self, value, expected):
        s = _settings_with({"RATE_LIMIT_ENABLED": value})
        assert s.rate_limit_enabled is expected


class TestDatabaseUrlValidator:
    """DATABASE_URL must start with postgresql (or sqlite when TESTING=1)."""

    def test_valid_postgresql(self):
        s = _settings_with({"DATABASE_URL": "postgresql://u:p@h/db"})
        assert s.database_url.startswith("postgresql")

    def test_invalid_mysql_raises(self):
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://u:p@h/db"}, clear=True):
            with pytest.raises(ValidationError, match="DATABASE_URL must start with"):
                _make()

    def test_sqlite_allowed_when_testing(self):
        s = _settings_with({"TESTING": "1", "DATABASE_URL": "sqlite://"})
        assert s.database_url == "sqlite://"

    def test_sqlite_rejected_when_not_testing(self):
        env = {"DATABASE_URL": "sqlite://", "TESTING": ""}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValidationError, match="DATABASE_URL must start with"):
                _make()


class TestSampleRateValidator:
    """Sentry sample rates must be 0.0-1.0."""

    @pytest.mark.parametrize("value", ["0.5", "0.0", "1.0"])
    def test_valid_rate(self, value):
        s = _settings_with({"SENTRY_TRACES_SAMPLE_RATE": value})
        assert s.sentry_traces_sample_rate == float(value)

    @pytest.mark.parametrize(
        ("env_key", "value"),
        [
            ("SENTRY_TRACES_SAMPLE_RATE", "1.5"),
            ("SENTRY_PROFILES_SAMPLE_RATE", "-0.1"),
        ],
    )
    def test_rate_out_of_range_raises(self, env_key, value):
        with patch.dict(os.environ, {env_key: value}, clear=True):
            with pytest.raises(ValidationError, match="Sample rate must be between"):
                _make()


class TestConfidenceValidator:
    """Confidence thresholds must be 0.0-1.0."""

    def test_valid_confidence(self):
        s = _settings_with({"MIN_TAG_CONFIDENCE": "0.85"})
        assert s.min_tag_confidence == 0.85

    def test_confidence_too_high(self):
        with patch.dict(os.environ, {"MIN_TAG_CONFIDENCE": "1.5"}, clear=True):
            with pytest.raises(ValidationError, match="Confidence/threshold must be between"):
                _make()


class TestCsvParsing:
    """Comma-separated env vars parse into lists correctly."""

    @pytest.mark.parametrize(
        ("env_key", "value", "attr", "expected"),
        [
            ("ADMIN_EMAILS", "a@b.com, c@d.com , E@F.COM", "admin_emails", ["a@b.com", "c@d.com", "e@f.com"]),
            ("ADMIN_EMAILS", "", "admin_emails", []),
            ("STOCK_SALE_VENDOR_NAMES", "Trio, Internal", "stock_sale_vendor_names", ["trio", "internal"]),
            ("STOCK_SALE_NOTIFY_EMAILS", "a@x.com,b@y.com", "stock_sale_notify_emails", ["a@x.com", "b@y.com"]),
            ("OWN_DOMAINS", "trioscs.com, example.com", "own_domains", frozenset({"trioscs.com", "example.com"})),
        ],
        ids=["admin_emails", "admin_emails_empty", "vendor_names", "notify_emails", "own_domains"],
    )
    def test_csv(self, env_key, value, attr, expected):
        s = _settings_with({env_key: value})
        assert getattr(s, attr) == expected


class TestTypeCoercion:
    """Integer and float env vars coerce from strings."""

    def test_int_from_string(self):
        s = _settings_with({"FOLLOW_UP_DAYS": "7"})
        assert s.follow_up_days == 7
        assert isinstance(s.follow_up_days, int)

    def test_float_from_string(self):
        s = _settings_with({"PROACTIVE_MIN_MARGIN_PCT": "25.5"})
        assert s.proactive_min_margin_pct == 25.5

    def test_invalid_int_raises(self):
        with patch.dict(os.environ, {"FOLLOW_UP_DAYS": "not-a-number"}, clear=True):
            with pytest.raises(ValidationError):
                _make()


class TestSecretKeyFallback:
    """SECRET_KEY env var maps to secret_key field."""

    def test_secret_key_direct(self):
        s = _settings_with({"SECRET_KEY": "my-secret"})
        assert s.secret_key == "my-secret"
