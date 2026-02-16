"""
test_routers_ai.py — Tests for AI Intelligence Layer Router

Tests _ai_enabled gate and _build_vendor_history helper.

Covers: ai feature flag modes (off/mike_only/all), vendor history aggregation
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _ai_enabled tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mike_user():
    u = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    return u


@pytest.fixture
def other_user():
    u = SimpleNamespace(email="buyer@trioscs.com", id=2, name="Buyer", role="buyer")
    return u


def _make_settings(flag: str, admin_emails: list[str] | None = None):
    s = SimpleNamespace(
        ai_features_enabled=flag,
        admin_emails=admin_emails or ["mike@trioscs.com"],
    )
    return s


def test_ai_enabled_off(mike_user):
    with patch("app.routers.ai.settings", _make_settings("off")):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(mike_user) is False


def test_ai_enabled_all(other_user):
    with patch("app.routers.ai.settings", _make_settings("all")):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(other_user) is True


def test_ai_enabled_mike_only_allows_mike(mike_user):
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings), \
         patch("app.dependencies.settings", mock_settings):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(mike_user) is True


def test_ai_enabled_mike_only_blocks_other(other_user):
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings), \
         patch("app.dependencies.settings", mock_settings):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(other_user) is False


def test_ai_enabled_mike_only_case_insensitive():
    user = SimpleNamespace(email="MIKE@TRIOSCS.COM", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings), \
         patch("app.dependencies.settings", mock_settings):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(user) is True


# ---------------------------------------------------------------------------
# _build_vendor_history tests
# ---------------------------------------------------------------------------

def test_build_vendor_history_no_card():
    """Unknown vendor returns empty dict."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with patch("app.routers.ai.normalize_vendor_name", return_value="acme"):
        from app.routers.ai import _build_vendor_history
        result = _build_vendor_history("Acme Corp", db)
    assert result == {}


def test_build_vendor_history_with_card():
    """Known vendor returns aggregated stats."""
    card = SimpleNamespace(
        engagement_score=78.5,
        response_velocity_hours=4.2,
    )
    last_contact = SimpleNamespace(
        created_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
    )

    db = MagicMock()
    # first query().filter().first() = card
    # second query().filter().count() = rfq count
    # third query().filter().count() = offer count
    # fourth query().filter().order_by().first() = last contact
    call_results = iter([card, 15, 3, last_contact])

    def side_effect(*a, **kw):
        mock = MagicMock()
        val = next(call_results)
        if isinstance(val, int):
            mock.filter.return_value.count.return_value = val
        elif hasattr(val, "created_at"):
            mock.filter.return_value.order_by.return_value.first.return_value = val
        else:
            mock.filter.return_value.first.return_value = val
        return mock

    db.query.side_effect = side_effect

    with patch("app.routers.ai.normalize_vendor_name", return_value="acme"):
        from app.routers.ai import _build_vendor_history
        result = _build_vendor_history("Acme Corp", db)

    assert result["total_rfqs"] == 15
    assert result["total_offers"] == 3
    assert result["last_contact_date"] == "2026-02-10"
    assert result["engagement_score"] == 78.5
