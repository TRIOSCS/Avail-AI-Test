"""Tests for credit_manager — monthly credit tracking per enrichment provider.

Covers: get_monthly_usage, can_use_credits, record_credit_usage, get_all_budgets.
"""

from unittest.mock import patch

import pytest

from tests.conftest import engine  # noqa: F401 — use test SQLite engine


@pytest.fixture
def _mock_settings():
    with patch("app.services.credit_manager.settings") as mock_s:
        mock_s.lusha_monthly_credit_limit = 300
        mock_s.lusha_phone_credit_limit = 210
        mock_s.lusha_discovery_credit_limit = 90
        mock_s.hunter_monthly_search_limit = 500
        mock_s.hunter_monthly_verify_limit = 500
        mock_s.apollo_monthly_credit_limit = 1000
        yield mock_s


def test_get_monthly_usage(db_session, _mock_settings):
    from app.services.credit_manager import get_monthly_usage

    usage = get_monthly_usage(db_session, "lusha")
    assert usage["provider"] == "lusha"
    assert usage["used"] == 0
    assert usage["limit"] == 300
    assert usage["remaining"] == 300


def test_can_use_credits_true(db_session, _mock_settings):
    from app.services.credit_manager import can_use_credits

    assert can_use_credits(db_session, "lusha", 1) is True


def test_can_use_credits_false(db_session, _mock_settings):
    from app.services.credit_manager import can_use_credits, record_credit_usage

    # Use up all credits
    for _ in range(300):
        record_credit_usage(db_session, "lusha", 1)
    db_session.flush()
    assert can_use_credits(db_session, "lusha", 1) is False


def test_record_credit_usage(db_session, _mock_settings):
    from app.services.credit_manager import get_monthly_usage, record_credit_usage

    record_credit_usage(db_session, "lusha", 5)
    db_session.flush()
    usage = get_monthly_usage(db_session, "lusha")
    assert usage["used"] == 5
    assert usage["remaining"] == 295


def test_get_all_budgets(db_session, _mock_settings):
    from app.services.credit_manager import get_all_budgets

    budgets = get_all_budgets(db_session)
    assert len(budgets) == 6  # lusha_phone, lusha_discovery, hunter_search, hunter_verify, apollo, lusha (aggregate)
    providers = [b["provider"] for b in budgets]
    assert "lusha_phone" in providers
    assert "lusha_discovery" in providers
    assert "lusha" in providers
    assert "hunter_search" in providers
    assert "hunter_verify" in providers
    assert "apollo" in providers


def test_credit_usage_incremental(db_session, _mock_settings):
    from app.services.credit_manager import get_monthly_usage, record_credit_usage

    record_credit_usage(db_session, "lusha", 3)
    record_credit_usage(db_session, "lusha", 7)
    db_session.flush()
    usage = get_monthly_usage(db_session, "lusha")
    assert usage["used"] == 10
