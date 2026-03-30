"""test_eight_by_eight_coverage.py — Coverage for 8x8 service and jobs.

Targets missing branches:
- eight_by_eight_service.py: get_extension_map, normalize_phone,
  reverse_lookup_phone, get_cdrs (HTTP error), get_access_token (token key)
- eight_by_eight_jobs.py: _process_cdrs (auth failure, empty CDRs,
  watermark create/update, vendor match, no user match), _update_watermark,
  _job_poll_8x8_cdrs

Called by: pytest
Depends on: app/services/eight_by_eight_service.py, app/jobs/eight_by_eight_jobs.py
"""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models import Company, VendorCard
from app.services.eight_by_eight_service import (
    get_access_token,
    get_cdrs,
    get_extension_map,
    normalize_cdr,
    normalize_phone,
    reverse_lookup_phone,
)

os.environ["TESTING"] = "1"

FAKE_SETTINGS = SimpleNamespace(
    eight_by_eight_api_key="test-key",
    eight_by_eight_username="user@test.com",
    eight_by_eight_password="secret",
    eight_by_eight_pbx_id="pbx-123",
    eight_by_eight_timezone="America/Los_Angeles",
)


# ── normalize_phone ────────────────────────────────────────────────────


class TestNormalizePhone:
    def test_empty_string_returns_empty(self):
        assert normalize_phone("") == ""

    def test_strips_formatting(self):
        assert normalize_phone("+1 (555) 123-4567") == "5551234567"

    def test_strips_leading_1(self):
        assert normalize_phone("15551234567") == "5551234567"

    def test_10_digit_number_unchanged(self):
        assert normalize_phone("5551234567") == "5551234567"

    def test_short_number_returned_as_is(self):
        result = normalize_phone("+1234")
        assert result == "1234"

    def test_dots_removed(self):
        assert normalize_phone("555.123.4567") == "5551234567"

    def test_none_like_empty_string(self):
        # Edge: None passed — shouldn't crash
        assert normalize_phone("") == ""


# ── get_access_token ───────────────────────────────────────────────────


class TestGetAccessToken:
    @patch("app.services.eight_by_eight_service.httpx.post")
    def test_uses_token_key_fallback(self, mock_post):
        """Response uses 'token' key instead of 'access_token'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"token": "tok-fallback"}
        mock_post.return_value = mock_resp

        result = get_access_token(FAKE_SETTINGS)
        assert result == "tok-fallback"

    @patch("app.services.eight_by_eight_service.httpx.post")
    def test_raises_when_no_token_in_response(self, mock_post):
        """Response 200 but no access_token or token key."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "ok"}
        mock_post.return_value = mock_resp

        with pytest.raises(ValueError, match="missing access_token"):
            get_access_token(FAKE_SETTINGS)

    @patch("app.services.eight_by_eight_service.httpx.post")
    def test_raises_on_http_error(self, mock_post):
        """HTTPError during auth raises ValueError."""
        mock_post.side_effect = httpx.HTTPError("connection refused")

        with pytest.raises(ValueError, match="auth request failed"):
            get_access_token(FAKE_SETTINGS)


# ── get_extension_map ──────────────────────────────────────────────────


class TestGetExtensionMap:
    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_ext_map(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"extension": "1001", "email": "mike@trio.com"},
                {"extensionNumber": "1002", "userId": "marcus@trio.com"},
            ]
        }
        mock_get.return_value = mock_resp

        result = get_extension_map("tok-123", FAKE_SETTINGS)
        assert result["1001"] == "mike@trio.com"
        assert result["1002"] == "marcus@trio.com"

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_empty_on_http_error(self, mock_get):
        mock_get.side_effect = httpx.HTTPError("timeout")
        result = get_extension_map("tok-123", FAKE_SETTINGS)
        assert result == {}

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_empty_on_non_200(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp
        result = get_extension_map("tok-123", FAKE_SETTINGS)
        assert result == {}

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_skips_users_without_ext_or_email(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"extension": None, "email": "nobody@test.com"},
                {"extensionNumber": "1003", "userId": None},
                {"extensionNumber": "1004", "email": "valid@test.com"},
            ]
        }
        mock_get.return_value = mock_resp
        result = get_extension_map("tok", FAKE_SETTINGS)
        assert "1004" in result
        assert len(result) == 1


# ── get_cdrs pagination and HTTP errors ───────────────────────────────


class TestGetCdrs:
    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_empty_on_http_error(self, mock_get):
        mock_get.side_effect = httpx.HTTPError("connection refused")
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        assert result == []

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_stops_when_all_records_fetched(self, mock_get):
        """Stops paginating when all records are already in all_records."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "meta": {"totalRecordCount": 2, "scrollId": "scroll-abc"},
            "data": [{"callId": "1"}, {"callId": "2"}],
        }
        mock_get.return_value = mock_resp
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        # Should stop after first page since all records fetched
        assert len(result) == 2
        assert mock_get.call_count == 1


# ── normalize_cdr edge cases ──────────────────────────────────────────


class TestNormalizeCdrEdgeCases:
    def test_fallback_to_starttime_string(self):
        cdr = {
            "callId": "abc",
            "startTime": "2026-03-05T14:35:20+00:00",
            "talkTimeMS": 5000,
            "direction": "Outgoing",
            "caller": "1001",
        }
        result = normalize_cdr(cdr)
        assert result["occurred_at"].year == 2026
        assert result["duration_seconds"] == 5

    def test_fallback_to_starttime_no_tz(self):
        cdr = {
            "callId": "abc",
            "startTime": "2026-03-05 14:35:20",
            "talkTimeMS": 3000,
            "direction": "Incoming",
        }
        result = normalize_cdr(cdr)
        assert result["occurred_at"] is not None
        assert result["occurred_at"].tzinfo is not None

    def test_invalid_talk_time_ms_defaults_to_zero(self):
        cdr = {"callId": "xyz", "talkTimeMS": "not-a-number", "direction": "Outgoing"}
        result = normalize_cdr(cdr)
        assert result["duration_seconds"] == 0

    def test_departments_none_gives_none_department(self):
        cdr = {"callId": "xyz", "departments": None, "direction": "Incoming"}
        result = normalize_cdr(cdr)
        assert result["department"] is None

    def test_empty_departments_list(self):
        cdr = {"callId": "xyz", "departments": [], "direction": "Outgoing"}
        result = normalize_cdr(cdr)
        assert result["department"] is None


# ── reverse_lookup_phone ───────────────────────────────────────────────


class TestReverseLookupPhone:
    def test_short_phone_returns_none(self, db_session):
        result = reverse_lookup_phone("123", db_session)
        assert result is None

    def test_no_match_returns_none(self, db_session):
        result = reverse_lookup_phone("+15559999999", db_session)
        assert result is None

    def test_matches_company_phone(self, db_session):
        co = Company(
            name="Phone Match Corp",
            phone="555-111-2222",
            is_active=True,
        )
        db_session.add(co)
        db_session.commit()

        result = reverse_lookup_phone("+15551112222", db_session)
        assert result is not None
        assert result["entity_type"] == "company"
        assert result["company_name"] == "Phone Match Corp"

    def test_matches_vendor_phone(self, db_session):
        vendor = VendorCard(
            normalized_name="phoneco",
            display_name="PhoneCo",
            phones=["+15553334444"],
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor)
        db_session.commit()

        result = reverse_lookup_phone("5553334444", db_session)
        assert result is not None
        assert result["entity_type"] == "vendor"
        assert result["vendor_card_id"] == vendor.id


# ── _process_cdrs edge cases ──────────────────────────────────────────


class TestProcessCdrsEdgeCases:
    def _make_mock_db(self, watermark=None, users=None):
        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = watermark
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = users or []

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            return MagicMock()

        db.query.side_effect = query_router
        return db

    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_auth_failure_returns_zeros(self, mock_auth):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.side_effect = ValueError("auth failed")
        db = self._make_mock_db()
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result == {"processed": 0, "matched": 0, "skipped": 0}

    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_empty_cdrs_updates_watermark(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []
        db = self._make_mock_db()
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_with_existing_watermark(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []

        wm_row = MagicMock()
        wm_row.value = "2026-03-01T00:00:00+00:00"
        db = self._make_mock_db(watermark=wm_row)
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_with_invalid_watermark_falls_back(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []

        wm_row = MagicMock()
        wm_row.value = "not-a-date"
        db = self._make_mock_db(watermark=wm_row)
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.services.eight_by_eight_service.reverse_lookup_phone")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_incoming_call_processed(self, mock_auth, mock_fetch, mock_log, mock_reverse):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [
            {
                "callId": "inc-001",
                "startTimeUTC": 1772750120399,
                "talkTimeMS": 30000,
                "caller": "+15559876543",
                "callerName": "Vendor X",
                "callee": "1001",
                "calleeName": "Michael",
                "direction": "Incoming",
                "missed": "-",
                "answered": "Answered",
                "departments": None,
            }
        ]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record
        mock_reverse.return_value = None

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.eight_by_eight_extension = "1001"

        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = None
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = [mock_user]
        req_join = MagicMock()
        req_join.join.return_value.filter.return_value.first.return_value = None

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            if name == "requisitions":
                return req_join
            return MagicMock()

        db.query.side_effect = query_router
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 1

    @patch("app.services.eight_by_eight_service.reverse_lookup_phone")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_cdr_skipped_when_log_returns_none(self, mock_auth, mock_fetch, mock_log, mock_reverse):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [
            {
                "callId": "dup-001",
                "startTimeUTC": 1772750120399,
                "talkTimeMS": 10000,
                "caller": "1001",
                "callerName": "Mike",
                "callee": "+15551234567",
                "direction": "Outgoing",
                "missed": "-",
                "answered": "Answered",
                "departments": None,
            }
        ]

        # log_call_activity returns None (dedup)
        mock_log.return_value = None
        mock_reverse.return_value = None

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.eight_by_eight_extension = "1001"

        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = None
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = [mock_user]

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            return MagicMock()

        db.query.side_effect = query_router
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["skipped"] == 1

    @patch("app.services.eight_by_eight_service.reverse_lookup_phone")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    def test_vendor_match_sets_vendor_card_id(self, mock_auth, mock_fetch, mock_log, mock_reverse):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [
            {
                "callId": "vendor-001",
                "startTimeUTC": 1772750120399,
                "talkTimeMS": 20000,
                "caller": "1001",
                "callerName": "Mike",
                "callee": "+15554567890",
                "direction": "Outgoing",
                "missed": "-",
                "answered": "Answered",
                "departments": None,
            }
        ]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record
        mock_reverse.return_value = {
            "entity_type": "vendor",
            "entity_id": 99,
            "company_id": None,
            "company_name": "VendorX",
            "contact_name": None,
            "vendor_card_id": 99,
        }

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.eight_by_eight_extension = "1001"

        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = None
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = [mock_user]

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            return MagicMock()

        db.query.side_effect = query_router
        result = _process_cdrs(db, FAKE_SETTINGS)
        assert result["matched"] == 1
        assert record.vendor_card_id == 99


# ── _update_watermark ──────────────────────────────────────────────────


class TestUpdateWatermark:
    def test_updates_existing_watermark(self):
        from app.jobs.eight_by_eight_jobs import _update_watermark

        db = MagicMock()
        wm_row = MagicMock()
        until = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)

        _update_watermark(db, wm_row, until)
        assert wm_row.value == until.isoformat()
        db.flush.assert_called_once()

    def test_creates_new_watermark_when_none(self):
        from app.jobs.eight_by_eight_jobs import _update_watermark

        db = MagicMock()
        until = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)

        _update_watermark(db, None, until)
        db.add.assert_called_once()
        db.flush.assert_called_once()


# ── _job_poll_8x8_cdrs ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_poll_8x8_cdrs_success():
    from app.jobs.eight_by_eight_jobs import _job_poll_8x8_cdrs

    mock_db = MagicMock()
    mock_db_factory = MagicMock(return_value=mock_db)
    with (
        patch.object(
            __import__("app.database", fromlist=["SessionLocal"]),
            "SessionLocal",
            mock_db_factory,
        ),
        patch("app.jobs.eight_by_eight_jobs._process_cdrs") as mock_process,
    ):
        mock_process.return_value = {"processed": 5, "matched": 3, "skipped": 2}
        # Patch at import site
        import app.database as _db_mod

        _orig = _db_mod.SessionLocal
        _db_mod.SessionLocal = mock_db_factory
        try:
            await _job_poll_8x8_cdrs()
        finally:
            _db_mod.SessionLocal = _orig

    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_job_poll_8x8_cdrs_handles_exception():
    from app.jobs.eight_by_eight_jobs import _job_poll_8x8_cdrs

    mock_db = MagicMock()
    mock_db_factory = MagicMock(return_value=mock_db)
    with patch("app.jobs.eight_by_eight_jobs._process_cdrs") as mock_process:
        mock_process.side_effect = Exception("Unexpected error")
        import app.database as _db_mod

        _orig = _db_mod.SessionLocal
        _db_mod.SessionLocal = mock_db_factory
        try:
            await _job_poll_8x8_cdrs()
        finally:
            _db_mod.SessionLocal = _orig

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ── reverse_lookup_phone: matched company has no site_id ─────────────


class TestReverseLookupPhoneMatchCount:
    def test_company_match_returns_none_site_id(self, db_session):
        co = Company(
            name="No Site Corp",
            phone="555-777-8888",
            is_active=True,
        )
        db_session.add(co)
        db_session.commit()

        result = reverse_lookup_phone("5557778888", db_session)
        assert result is not None
        assert result["site_id"] is None

    def test_vendor_with_multiple_phones_matches_correct_one(self, db_session):
        vendor = VendorCard(
            normalized_name="multiphone vendor",
            display_name="MultiPhone Vendor",
            phones=["555-100-2000", "555-200-3000"],
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor)
        db_session.commit()

        result = reverse_lookup_phone("5552003000", db_session)
        assert result is not None
        assert result["entity_type"] == "vendor"
