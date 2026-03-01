"""Tests for Phase 3 — Maximize Graph API Usage.

Covers:
  3A: Mailbox settings (timezone, working hours) fetch and storage
  3B: OOO detection columns on VendorContact
  3C: Calendar intelligence — vendor meetings and trade shows

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import engine  # noqa: F401

# ═══════════════════════════════════════════════════════════════════════
#  3A: Mailbox Settings
# ═══════════════════════════════════════════════════════════════════════


class TestMailboxIntelligence:
    def test_fetch_and_store_mailbox_settings(self, db_session, test_user):
        """Fetches timezone and working hours from Graph and stores on User."""
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "timeZone": "Eastern Standard Time",
                "workingHours": {
                    "startTime": "08:00:00.0000000",
                    "endTime": "17:00:00.0000000",
                    "daysOfWeek": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                    "timeZone": {"name": "Eastern Standard Time"},
                },
                "automaticRepliesSetting": {
                    "status": "disabled",
                },
            }
        )

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                fetch_and_store_mailbox_settings("fake-token", test_user, db_session)
            )

        assert result is not None
        assert test_user.timezone == "Eastern Standard Time"
        assert test_user.working_hours_start == "08:00"
        assert test_user.working_hours_end == "17:00"
        assert result["auto_reply_status"] == "disabled"

    def test_fetch_mailbox_settings_graph_error(self, db_session, test_user):
        """Returns None on Graph API failure."""
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("Graph API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                fetch_and_store_mailbox_settings("fake-token", test_user, db_session)
            )

        assert result is None

    def test_fetch_mailbox_settings_empty_response(self, db_session, test_user):
        """Returns None on empty/error response."""
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"error": {"code": "ErrorAccessDenied"}})

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                fetch_and_store_mailbox_settings("fake-token", test_user, db_session)
            )

        assert result is None

    def test_is_within_working_hours(self, db_session, test_user):
        """Checks if a given hour falls within working hours."""
        from app.services.mailbox_intelligence import is_within_working_hours

        test_user.working_hours_start = "08:00"
        test_user.working_hours_end = "17:00"

        assert is_within_working_hours(test_user, 10) is True
        assert is_within_working_hours(test_user, 20) is False
        assert is_within_working_hours(test_user, 8) is True
        assert is_within_working_hours(test_user, 17) is False

    def test_is_within_working_hours_no_config(self, db_session, test_user):
        """Returns True if working hours not configured."""
        from app.services.mailbox_intelligence import is_within_working_hours

        test_user.working_hours_start = None
        test_user.working_hours_end = None

        assert is_within_working_hours(test_user, 10) is True


# ═══════════════════════════════════════════════════════════════════════
#  3B: OOO Detection
# ═══════════════════════════════════════════════════════════════════════


class TestOOODetection:
    def test_vendor_contact_ooo_columns(self, db_session, test_vendor_card):
        """VendorContact has is_ooo and ooo_return_date columns."""
        from app.models import VendorContact

        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="Away Person",
            email="away@vendor.com",
            source="email_mining",
            is_ooo=True,
            ooo_return_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        db_session.add(vc)
        db_session.commit()

        fetched = db_session.query(VendorContact).filter_by(email="away@vendor.com").first()
        assert fetched.is_ooo is True
        assert fetched.ooo_return_date is not None

    def test_vendor_contact_ooo_default_false(self, db_session, test_vendor_card):
        """is_ooo defaults to False."""
        from app.models import VendorContact

        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="Active Person",
            email="active@vendor.com",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()

        assert vc.is_ooo is False
        assert vc.ooo_return_date is None


# ═══════════════════════════════════════════════════════════════════════
#  3C: Calendar Intelligence
# ═══════════════════════════════════════════════════════════════════════


class TestCalendarIntelligence:
    def test_scan_calendar_detects_vendor_meeting(self, db_session, test_user):
        """Calendar scan detects meetings with external (vendor) attendees."""
        from app.services.calendar_intelligence import scan_calendar_events

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "subject": "Quarterly Business Review",
                    "attendees": [
                        {
                            "emailAddress": {"address": "rep@arrow.com", "name": "Arrow Rep"},
                            "type": "required",
                        },
                        {
                            "emailAddress": {"address": "buyer@trioscs.com", "name": "Our Buyer"},
                            "type": "required",
                        },
                    ],
                    "start": {"dateTime": "2026-02-20T10:00:00"},
                    "end": {"dateTime": "2026-02-20T11:00:00"},
                    "location": {"displayName": "Zoom"},
                    "organizer": {"emailAddress": {"address": "buyer@trioscs.com"}},
                },
            ]
        )

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session, lookback_days=30)
            )

        assert result["events_scanned"] == 1
        assert result["vendor_meetings"] == 1
        assert result["activities_logged"] == 1

    def test_scan_calendar_detects_trade_show(self, db_session, test_user):
        """Calendar scan detects trade show events by keyword."""
        from app.services.calendar_intelligence import scan_calendar_events

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "subject": "Electronica 2026 Munich",
                    "attendees": [],
                    "start": {"dateTime": "2026-11-12T09:00:00"},
                    "end": {"dateTime": "2026-11-15T17:00:00"},
                    "location": {"displayName": "Messe München"},
                    "organizer": {},
                },
            ]
        )

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session, lookback_days=365)
            )

        assert result["trade_shows"] == 1

    def test_scan_calendar_no_internal_meetings(self, db_session, test_user):
        """Internal meetings (only own-domain attendees) are not logged."""
        from app.services.calendar_intelligence import scan_calendar_events

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "subject": "Team Standup",
                    "attendees": [
                        {"emailAddress": {"address": "colleague@trioscs.com", "name": "Colleague"}},
                    ],
                    "start": {"dateTime": "2026-02-25T09:00:00"},
                    "end": {"dateTime": "2026-02-25T09:15:00"},
                    "location": {},
                    "organizer": {},
                },
            ]
        )

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session, lookback_days=30)
            )

        assert result["vendor_meetings"] == 0
        assert result["activities_logged"] == 0

    def test_scan_calendar_graph_failure(self, db_session, test_user):
        """Returns empty result on Graph API failure."""
        from app.services.calendar_intelligence import scan_calendar_events

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(side_effect=Exception("Calendar API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session)
            )

        assert result["events_scanned"] == 0

    def test_scan_calendar_dedup(self, db_session, test_user):
        """Same event is not logged twice."""
        from app.services.calendar_intelligence import scan_calendar_events

        event = {
            "subject": "Vendor Call",
            "attendees": [
                {"emailAddress": {"address": "rep@vendor.com", "name": "Rep"}},
            ],
            "start": {"dateTime": "2026-02-20T10:00:00"},
            "end": {"dateTime": "2026-02-20T11:00:00"},
            "location": {},
            "organizer": {},
        }

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[event])

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            # First scan
            r1 = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session, lookback_days=30)
            )
            # Second scan (same event)
            r2 = asyncio.get_event_loop().run_until_complete(
                scan_calendar_events("fake-token", test_user.id, db_session, lookback_days=30)
            )

        assert r1["activities_logged"] == 1
        assert r2["activities_logged"] == 0  # dedup


# ═══════════════════════════════════════════════════════════════════════
#  User model columns
# ═══════════════════════════════════════════════════════════════════════


class TestUserMailboxColumns:
    def test_user_has_timezone_column(self, db_session, test_user):
        """User model has timezone, working_hours_start/end columns."""
        test_user.timezone = "Pacific Standard Time"
        test_user.working_hours_start = "09:00"
        test_user.working_hours_end = "18:00"
        db_session.commit()

        from app.models import User

        fetched = db_session.query(User).get(test_user.id)
        assert fetched.timezone == "Pacific Standard Time"
        assert fetched.working_hours_start == "09:00"
        assert fetched.working_hours_end == "18:00"
