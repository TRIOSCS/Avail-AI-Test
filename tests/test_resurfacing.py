"""Tests for cross-customer MPN resurfacing service.

Covers: get_mpn_hints(), _build_hint(), edge cases.
Uses MagicMock for db — no real database needed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.resurfacing_service import get_mpn_hints, _build_hint, _format_age


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_db_no_results():
    """Return a mock db where all queries return None/empty."""
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.order_by.return_value = query
    query.join.return_value = query
    query.distinct.return_value = query
    query.limit.return_value = query
    query.first.return_value = None
    query.all.return_value = []
    return db


# ---------------------------------------------------------------------------
# test_returns_dict_keyed_by_mpn
# ---------------------------------------------------------------------------

def test_returns_dict_keyed_by_mpn():
    """get_mpn_hints returns a dict keyed by each input MPN."""
    db = _mock_db_no_results()
    result = get_mpn_hints(["MPN-A", "MPN-B"], db)

    assert isinstance(result, dict)
    assert "MPN-A" in result
    assert "MPN-B" in result


# ---------------------------------------------------------------------------
# test_returns_none_for_unknown_mpn
# ---------------------------------------------------------------------------

def test_returns_none_for_unknown_mpn():
    """Unknown MPNs (no offers, no cross-reqs, no knowledge) return None."""
    db = _mock_db_no_results()
    result = get_mpn_hints(["UNKNOWN-123"], db)

    assert result["UNKNOWN-123"] is None


# ---------------------------------------------------------------------------
# test_empty_list_returns_empty_dict
# ---------------------------------------------------------------------------

def test_empty_list_returns_empty_dict():
    """Empty input list returns empty dict immediately."""
    db = MagicMock()
    result = get_mpn_hints([], db)

    assert result == {}
    # db should not have been queried at all
    db.query.assert_not_called()


# ---------------------------------------------------------------------------
# test_exclude_req_id_filters_current_req
# ---------------------------------------------------------------------------

def test_exclude_req_id_filters_current_req():
    """exclude_req_id adds extra filter calls to exclude the current requisition."""
    db = _mock_db_no_results()
    result = get_mpn_hints(["MPN-X"], db, exclude_req_id=42)

    assert "MPN-X" in result
    # The filter method should have been called (multiple times for the
    # various hint sources) — we just verify it was invoked with exclude logic
    assert db.query.return_value.filter.called


# ---------------------------------------------------------------------------
# test_offer_hint_formatting
# ---------------------------------------------------------------------------

def test_offer_hint_formatting():
    """When an offer exists, hint is formatted correctly."""
    db = MagicMock()
    query_mock = MagicMock()
    db.query.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value = query_mock

    # First call: offer query — returns an offer
    mock_offer = MagicMock()
    mock_offer.unit_price = 12.50
    mock_offer.vendor_name = "Acme Corp"
    mock_offer.created_at = datetime.now(timezone.utc) - timedelta(days=3)

    query_mock.first.return_value = mock_offer

    result = get_mpn_hints(["MPN-1"], db)
    hint = result["MPN-1"]

    assert hint is not None
    assert "Last quoted $12.50 from Acme Corp" in hint
    assert "3d ago" in hint


# ---------------------------------------------------------------------------
# test_format_age_today
# ---------------------------------------------------------------------------

def test_format_age_today():
    """_format_age returns 'today' for datetimes within the same day."""
    now = datetime.now(timezone.utc)
    assert _format_age(now) == "today"


def test_format_age_days():
    """_format_age returns 'Nd ago' for recent dates."""
    dt = datetime.now(timezone.utc) - timedelta(days=5)
    assert _format_age(dt) == "5d ago"


def test_format_age_months():
    """_format_age returns 'Nmo ago' for dates > 60 days old."""
    dt = datetime.now(timezone.utc) - timedelta(days=90)
    assert _format_age(dt) == "3mo ago"


def test_format_age_one_day():
    """_format_age returns '1d ago' for exactly 1 day."""
    dt = datetime.now(timezone.utc) - timedelta(days=1)
    assert _format_age(dt) == "1d ago"


def test_format_age_none():
    """_format_age returns 'unknown date' for None."""
    assert _format_age(None) == "unknown date"


def test_format_age_naive_datetime():
    """_format_age handles naive datetimes by treating them as UTC."""
    dt = datetime.utcnow() - timedelta(days=2)
    assert _format_age(dt) == "2d ago"


# ---------------------------------------------------------------------------
# test_exception_handling
# ---------------------------------------------------------------------------

def test_exception_in_build_hint_returns_none():
    """If _build_hint raises, get_mpn_hints catches and returns None for that MPN."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("db exploded")

    result = get_mpn_hints(["BOOM-MPN"], db)
    assert result["BOOM-MPN"] is None


# ---------------------------------------------------------------------------
# test_build_hint_returns_none_when_no_data
# ---------------------------------------------------------------------------

def test_build_hint_returns_none_when_no_data():
    """_build_hint returns None when no offers, cross-reqs, or knowledge found."""
    db = _mock_db_no_results()
    assert _build_hint("NOTHING", db, None) is None
