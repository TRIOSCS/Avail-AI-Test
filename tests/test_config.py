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


class TestDefaults:
    """Default values apply when env vars are absent."""

    def test_default_app_url(self):
        with patch.dict(os.environ, {}, clear=True):
            s = _make()
        assert s.app_url == "http://localhost:8000"

    def test_default_database_url(self):
        with patch.dict(os.environ, {}, clear=True):
            s = _make()
        assert s.database_url == "postgresql://availai:availai@db:5432/availai"

    def test_default_rate_limit_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is True

    def test_default_admin_emails_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            s = _make()
        assert s.admin_emails == []

    def test_default_own_domains(self):
        with patch.dict(os.environ, {}, clear=True):
            s = _make()
        assert s.own_domains == frozenset({"trioscs.com"})


class TestBooleanCoercion:
    """Boolean env vars coerce string values correctly."""

    def test_true_lowercase(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "true"}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is True

    def test_true_uppercase(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "TRUE"}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is True

    def test_false_string(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "false"}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is False

    def test_one_is_true(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "1"}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is True

    def test_zero_is_false(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "0"}, clear=True):
            s = _make()
        assert s.rate_limit_enabled is False


class TestDatabaseUrlValidator:
    """DATABASE_URL must start with postgresql (or sqlite when TESTING=1)."""

    def test_valid_postgresql(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@h/db"}, clear=True):
            s = _make()
        assert s.database_url.startswith("postgresql")

    def test_invalid_mysql_raises(self):
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://u:p@h/db"}, clear=True):
            with pytest.raises(ValidationError, match="DATABASE_URL must start with"):
                _make()

    def test_sqlite_allowed_when_testing(self):
        with patch.dict(os.environ, {"TESTING": "1", "DATABASE_URL": "sqlite://"}, clear=True):
            s = _make()
        assert s.database_url == "sqlite://"

    def test_sqlite_rejected_when_not_testing(self):
        env = {"DATABASE_URL": "sqlite://", "TESTING": ""}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValidationError, match="DATABASE_URL must start with"):
                _make()


class TestSampleRateValidator:
    """Sentry sample rates must be 0.0-1.0."""

    def test_valid_rate(self):
        with patch.dict(os.environ, {"SENTRY_TRACES_SAMPLE_RATE": "0.5"}, clear=True):
            s = _make()
        assert s.sentry_traces_sample_rate == 0.5

    def test_rate_too_high(self):
        with patch.dict(os.environ, {"SENTRY_TRACES_SAMPLE_RATE": "1.5"}, clear=True):
            with pytest.raises(ValidationError, match="Sample rate must be between"):
                _make()

    def test_rate_negative(self):
        with patch.dict(os.environ, {"SENTRY_PROFILES_SAMPLE_RATE": "-0.1"}, clear=True):
            with pytest.raises(ValidationError, match="Sample rate must be between"):
                _make()

    def test_rate_zero_is_valid(self):
        with patch.dict(os.environ, {"SENTRY_TRACES_SAMPLE_RATE": "0.0"}, clear=True):
            s = _make()
        assert s.sentry_traces_sample_rate == 0.0

    def test_rate_one_is_valid(self):
        with patch.dict(os.environ, {"SENTRY_TRACES_SAMPLE_RATE": "1.0"}, clear=True):
            s = _make()
        assert s.sentry_traces_sample_rate == 1.0


class TestConfidenceValidator:
    """Confidence thresholds must be 0.0-1.0."""

    def test_valid_confidence(self):
        with patch.dict(os.environ, {"MIN_TAG_CONFIDENCE": "0.85"}, clear=True):
            s = _make()
        assert s.min_tag_confidence == 0.85

    def test_confidence_too_high(self):
        with patch.dict(os.environ, {"MIN_TAG_CONFIDENCE": "1.5"}, clear=True):
            with pytest.raises(ValidationError, match="Confidence/threshold must be between"):
                _make()


class TestCsvParsing:
    """Comma-separated env vars parse into lists correctly."""

    def test_admin_emails_csv(self):
        with patch.dict(os.environ, {"ADMIN_EMAILS": "a@b.com, c@d.com , E@F.COM"}, clear=True):
            s = _make()
        assert s.admin_emails == ["a@b.com", "c@d.com", "e@f.com"]

    def test_admin_emails_empty_string(self):
        with patch.dict(os.environ, {"ADMIN_EMAILS": ""}, clear=True):
            s = _make()
        assert s.admin_emails == []

    def test_stock_sale_vendor_names_csv(self):
        with patch.dict(os.environ, {"STOCK_SALE_VENDOR_NAMES": "Trio, Internal"}, clear=True):
            s = _make()
        assert s.stock_sale_vendor_names == ["trio", "internal"]

    def test_stock_sale_notify_emails_csv(self):
        with patch.dict(os.environ, {"STOCK_SALE_NOTIFY_EMAILS": "a@x.com,b@y.com"}, clear=True):
            s = _make()
        assert s.stock_sale_notify_emails == ["a@x.com", "b@y.com"]

    def test_own_domains_csv(self):
        with patch.dict(os.environ, {"OWN_DOMAINS": "trioscs.com, example.com"}, clear=True):
            s = _make()
        assert s.own_domains == frozenset({"trioscs.com", "example.com"})


class TestTypeCoercion:
    """Integer and float env vars coerce from strings."""

    def test_int_from_string(self):
        with patch.dict(os.environ, {"FOLLOW_UP_DAYS": "7"}, clear=True):
            s = _make()
        assert s.follow_up_days == 7
        assert isinstance(s.follow_up_days, int)

    def test_float_from_string(self):
        with patch.dict(os.environ, {"TEAMS_HOT_THRESHOLD": "25000.50"}, clear=True):
            s = _make()
        assert s.teams_hot_threshold == 25000.50

    def test_invalid_int_raises(self):
        with patch.dict(os.environ, {"FOLLOW_UP_DAYS": "not-a-number"}, clear=True):
            with pytest.raises(ValidationError):
                _make()


class TestSecretKeyFallback:
    """SECRET_KEY env var maps to secret_key field."""

    def test_secret_key_direct(self):
        with patch.dict(os.environ, {"SECRET_KEY": "my-secret"}, clear=True):
            s = _make()
        assert s.secret_key == "my-secret"
