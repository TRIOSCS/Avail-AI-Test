"""
test_calendar.py — Calendar availability service tests.

Tests OOO detection, normal day availability, caching, API error
graceful degradation, and keyword matching. All Graph API calls are mocked.

Called by: pytest
Depends on: app/services/calendar.py, conftest.py
"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.services.calendar import (
    _OOO_KEYWORDS,
    _check_calendar,
    clear_cache,
    is_buyer_available,
)


@pytest.fixture(autouse=True)
def _clear_calendar_cache():
    """Ensure clean cache for each test."""
    clear_cache()
    yield
    clear_cache()


# ── is_buyer_available ────────────────────────────────────────────────


class TestIsBuyerAvailable:
    @pytest.mark.asyncio
    async def test_available_no_events(self, db_session, test_user):
        with patch("app.services.calendar._check_calendar", new_callable=AsyncMock, return_value=True):
            result = await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_unavailable_ooo(self, db_session, test_user):
        with patch("app.services.calendar._check_calendar", new_callable=AsyncMock, return_value=False):
            result = await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            assert result is False

    @pytest.mark.asyncio
    async def test_caches_result(self, db_session, test_user):
        mock = AsyncMock(return_value=True)
        with patch("app.services.calendar._check_calendar", mock):
            await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            # Should only call the API once
            mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_different_dates_not_cached(self, db_session, test_user):
        mock = AsyncMock(return_value=True)
        with patch("app.services.calendar._check_calendar", mock):
            await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            await is_buyer_available(test_user.id, date(2026, 2, 18), db_session)
            assert mock.await_count == 2

    @pytest.mark.asyncio
    async def test_api_error_returns_true(self, db_session, test_user):
        """Graceful degradation: API failure → assume available."""
        with patch("app.services.calendar._check_calendar", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_error_result_cached(self, db_session, test_user):
        """Error result (True) should be cached so we don't retry repeatedly."""
        mock = AsyncMock(side_effect=Exception("fail"))
        with patch("app.services.calendar._check_calendar", mock):
            r1 = await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            r2 = await is_buyer_available(test_user.id, date(2026, 2, 17), db_session)
            assert r1 is True
            assert r2 is True
            mock.assert_awaited_once()  # Cached after first error


# ── _check_calendar ───────────────────────────────────────────────────


class TestCheckCalendar:
    @pytest.mark.asyncio
    async def test_no_token_returns_true(self, db_session, test_user):
        """User without access_token → assume available."""
        test_user.access_token = None
        db_session.commit()

        result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
        assert result is True

    @pytest.mark.asyncio
    async def test_oof_event_returns_false(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "Out of Office", "showAs": "oof", "isAllDay": True},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is False

    @pytest.mark.asyncio
    async def test_all_day_busy_returns_false(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "Company Holiday", "showAs": "busy", "isAllDay": True},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is False

    @pytest.mark.asyncio
    async def test_pto_keyword_returns_false(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "PTO - Beach vacation", "showAs": "free", "isAllDay": False},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is False

    @pytest.mark.asyncio
    async def test_normal_meeting_returns_true(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "Team standup", "showAs": "busy", "isAllDay": False},
            {"subject": "1:1 with manager", "showAs": "tentative", "isAllDay": False},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_empty_calendar_returns_true(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value={"value": []})
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_api_error_in_response_returns_true(self, db_session, test_user):
        """Graph API returns error object → graceful degradation."""
        test_user.access_token = "token-123"
        db_session.commit()

        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value={"error": {"message": "Forbidden"}})
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_vacation_keyword_case_insensitive(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "VACATION - Hawaii", "showAs": "free", "isAllDay": False},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is False

    @pytest.mark.asyncio
    async def test_sick_keyword_returns_false(self, db_session, test_user):
        test_user.access_token = "token-123"
        db_session.commit()

        events = {"value": [
            {"subject": "Sick day", "showAs": "free", "isAllDay": True},
        ]}
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.get_json = AsyncMock(return_value=events)
            MockGC.return_value = instance

            result = await _check_calendar(test_user.id, date(2026, 2, 17), db_session)
            assert result is False


# ── OOO Keywords ──────────────────────────────────────────────────────


class TestOOOKeywords:
    def test_expected_keywords_present(self):
        expected = {"pto", "vacation", "ooo", "out of office", "holiday", "sick", "leave"}
        assert _OOO_KEYWORDS == expected

    def test_keywords_are_lowercase(self):
        for kw in _OOO_KEYWORDS:
            assert kw == kw.lower()


# ── Routing integration ───────────────────────────────────────────────


class TestRoutingIntegration:
    @pytest.mark.asyncio
    async def test_rank_with_availability_filters_ooo(self, db_session):
        """rank_buyers_with_availability excludes OOO buyers."""
        from app.services.routing_service import rank_buyers_with_availability

        # Mock rank_buyers_for_assignment to return two buyers
        fake_ranked = [
            {"user_id": 1, "user_name": "Alice", "score_details": {"total": 80}},
            {"user_id": 2, "user_name": "Bob", "score_details": {"total": 60}},
        ]
        with patch("app.services.routing_service.rank_buyers_for_assignment", return_value=fake_ranked), \
             patch("app.services.calendar.is_buyer_available", new_callable=AsyncMock) as mock_avail:
            # Alice is OOO, Bob is available
            mock_avail.side_effect = [False, True]
            result = await rank_buyers_with_availability(1, 1, db_session)
            assert len(result) == 1
            assert result[0]["user_name"] == "Bob"

    @pytest.mark.asyncio
    async def test_rank_all_ooo_falls_back(self, db_session):
        """If ALL buyers are OOO, return full list (don't leave nobody)."""
        from app.services.routing_service import rank_buyers_with_availability

        fake_ranked = [
            {"user_id": 1, "user_name": "Alice", "score_details": {"total": 80}},
            {"user_id": 2, "user_name": "Bob", "score_details": {"total": 60}},
        ]
        with patch("app.services.routing_service.rank_buyers_for_assignment", return_value=fake_ranked), \
             patch("app.services.calendar.is_buyer_available", new_callable=AsyncMock, return_value=False):
            result = await rank_buyers_with_availability(1, 1, db_session)
            assert len(result) == 2  # Falls back to full list

    @pytest.mark.asyncio
    async def test_rank_calendar_error_returns_all(self, db_session):
        """Calendar service failure → return all buyers (graceful degradation)."""
        from app.services.routing_service import rank_buyers_with_availability

        fake_ranked = [
            {"user_id": 1, "user_name": "Alice", "score_details": {"total": 80}},
        ]
        with patch("app.services.routing_service.rank_buyers_for_assignment", return_value=fake_ranked), \
             patch("app.services.calendar.is_buyer_available", new_callable=AsyncMock, side_effect=Exception("fail")):
            result = await rank_buyers_with_availability(1, 1, db_session)
            assert len(result) == 1
