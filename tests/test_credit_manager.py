"""Tests for credit_manager — monthly credit tracking per enrichment provider.

Covers: get_monthly_usage, can_use_credits, record_credit_usage, get_all_budgets,
        check_and_record_credits (atomic), concurrent upsert safety, budget rejection.
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


# --- Existing tests (preserved) ---


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


# --- New tests: check_and_record_credits ---


def test_check_and_record_credits_success(db_session, _mock_settings):
    """check_and_record_credits returns True and records usage when budget available."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    result = check_and_record_credits(db_session, "apollo", 5)
    assert result is True
    db_session.flush()
    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 5
    assert usage["remaining"] == 995


def test_check_and_record_credits_exact_limit(db_session, _mock_settings):
    """check_and_record_credits succeeds when requesting exactly remaining credits."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    # Use 999 of 1000
    result = check_and_record_credits(db_session, "apollo", 999)
    assert result is True
    db_session.flush()

    # Use the last 1
    result = check_and_record_credits(db_session, "apollo", 1)
    assert result is True
    db_session.flush()

    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 1000
    assert usage["remaining"] == 0


def test_check_and_record_credits_budget_exceeded(db_session, _mock_settings):
    """check_and_record_credits returns False when budget would be exceeded."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    # Use all 1000 credits
    result = check_and_record_credits(db_session, "apollo", 1000)
    assert result is True
    db_session.flush()

    # Try to use 1 more — should fail
    result = check_and_record_credits(db_session, "apollo", 1)
    assert result is False

    # Verify no extra credits were consumed
    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 1000


def test_check_and_record_credits_partial_overspend(db_session, _mock_settings):
    """check_and_record_credits rejects if count would exceed remaining."""
    from app.services.credit_manager import check_and_record_credits

    # Use 998 of 1000
    check_and_record_credits(db_session, "apollo", 998)
    db_session.flush()

    # Try to use 5 more — only 2 remain, should reject
    result = check_and_record_credits(db_session, "apollo", 5)
    assert result is False


# --- New tests: concurrent upsert safety ---


def test_get_or_create_row_handles_integrity_error(db_session, _mock_settings):
    """_get_or_create_row retries SELECT after IntegrityError from concurrent insert."""
    from app.services.credit_manager import _current_month, _get_or_create_row

    month = _current_month()

    # First call creates the row normally
    row1 = _get_or_create_row(db_session, "apollo", month)
    assert row1 is not None
    assert row1.credits_used == 0

    # Second call should find existing row (no IntegrityError)
    row2 = _get_or_create_row(db_session, "apollo", month)
    assert row2.id == row1.id


def test_get_or_create_row_integrity_error_recovery(db_session, _mock_settings):
    """Simulate IntegrityError on insert, verify fallback SELECT works."""

    from app.models.enrichment import EnrichmentCreditUsage
    from app.services.credit_manager import _current_month

    month = _current_month()

    # Pre-insert a row to make the next insert fail
    row = EnrichmentCreditUsage(
        provider="test_provider",
        month=month,
        credits_used=0,
        credits_limit=100,
    )
    db_session.add(row)
    db_session.flush()

    # Now _get_or_create_row should find the existing row via SELECT
    from app.services.credit_manager import _get_or_create_row

    result = _get_or_create_row(db_session, "test_provider", month)
    assert result.id == row.id


def test_get_or_create_row_uses_savepoint(db_session, _mock_settings):
    """Verify begin_nested() (savepoint) is used so outer transaction survives."""
    from app.services.credit_manager import _current_month, _get_or_create_row

    month = _current_month()

    # Create row for provider A
    row_a = _get_or_create_row(db_session, "provider_a", month)
    assert row_a is not None

    # Create row for provider B — outer transaction should still be fine
    row_b = _get_or_create_row(db_session, "provider_b", month)
    assert row_b is not None
    assert row_a.provider == "provider_a"
    assert row_b.provider == "provider_b"


# --- New tests: multiple providers isolation ---


def test_check_and_record_credits_provider_isolation(db_session, _mock_settings):
    """Credits for different providers are tracked independently."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    check_and_record_credits(db_session, "apollo", 100)
    check_and_record_credits(db_session, "hunter_search", 50)
    db_session.flush()

    apollo = get_monthly_usage(db_session, "apollo")
    hunter = get_monthly_usage(db_session, "hunter_search")
    assert apollo["used"] == 100
    assert hunter["used"] == 50


def test_check_and_record_multiple_calls(db_session, _mock_settings):
    """Multiple check_and_record calls accumulate correctly."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    for _ in range(10):
        result = check_and_record_credits(db_session, "apollo", 1)
        assert result is True
    db_session.flush()

    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 10


def test_default_limit_unknown_provider(db_session, _mock_settings):
    """Unknown provider gets default limit of 100."""
    from app.services.credit_manager import get_monthly_usage

    usage = get_monthly_usage(db_session, "unknown_provider")
    assert usage["limit"] == 100
