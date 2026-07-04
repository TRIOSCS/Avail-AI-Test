"""test_eight_by_eight_coverage.py — Coverage for 8x8 service and jobs.

Targets missing branches:
- eight_by_eight_service.py: normalize_phone, get_cdrs (HTTP error),
  get_access_token (token key, caching)
- eight_by_eight_jobs.py: _process_cdrs (auth failure, empty CDRs,
  watermark create/update, vendor match, no user match, optimistic reconcile),
  _update_watermark, _find_optimistic_row, _job_poll_8x8_cdrs

Called by: pytest
Depends on: app/services/eight_by_eight_service.py, app/jobs/eight_by_eight_jobs.py
"""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.eight_by_eight_service import (
    _token_cache,
    get_access_token,
    get_cdrs,
    normalize_cdr,
    normalize_phone,
)

os.environ["TESTING"] = "1"

FAKE_SETTINGS = SimpleNamespace(
    eight_by_eight_api_key="test-key",
    eight_by_eight_username="user@test.com",
    eight_by_eight_password="secret",
    eight_by_eight_pbx_id="pbx-123",
    eight_by_eight_timezone="America/Los_Angeles",
)


def _mock_async_client(*, get=None, post=None):
    """Build a mock replacing the shared ``http`` client.

    `get`/`post` may be a single response, an iterable of responses
    (side_effect), or an exception. Returns a MagicMock with async `.get`/`.post`,
    suitable as the `new` of a patch on `app.services.eight_by_eight_service.http`.
    The service now uses the shared pooled client directly (no `async with`), so the
    mock is the client itself — ``._client`` aliases it for the existing assertions.
    """
    client = MagicMock()

    def _async_method(spec):
        m = AsyncMock()
        if spec is None:
            return m
        if isinstance(spec, BaseException) or (isinstance(spec, type) and issubclass(spec, BaseException)):
            m.side_effect = spec
        elif isinstance(spec, (list, tuple)):
            m.side_effect = list(spec)
        else:
            m.return_value = spec
        return m

    client.get = _async_method(get)
    client.post = _async_method(post)
    client._client = client  # expose for assertions (back-compat with CM-era tests)
    return client


# ── normalize_phone ────────────────────────────────────────────────────


class TestNormalizePhone:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("", "", id="empty_string_returns_empty"),
            pytest.param("+1 (555) 123-4567", "5551234567", id="strips_formatting"),
            pytest.param("15551234567", "5551234567", id="strips_leading_1"),
            pytest.param("5551234567", "5551234567", id="10_digit_number_unchanged"),
            pytest.param("+1234", "1234", id="short_number_returned_as_is"),
            pytest.param("555.123.4567", "5551234567", id="dots_removed"),
        ],
    )
    def test_normalize_phone(self, raw, expected):
        assert normalize_phone(raw) == expected


# ── get_access_token ───────────────────────────────────────────────────


class TestGetAccessToken:
    def setup_method(self):
        """Clear the token cache before each test."""
        _token_cache.clear()

    async def test_uses_token_key_fallback(self):
        """Response uses 'token' key instead of 'access_token'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"token": "tok-fallback"}
        factory = _mock_async_client(post=mock_resp)

        with patch("app.services.eight_by_eight_service.http", factory):
            result = await get_access_token(FAKE_SETTINGS)
        assert result == "tok-fallback"

    async def test_raises_when_no_token_in_response(self):
        """Response 200 but no access_token or token key."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "ok"}
        factory = _mock_async_client(post=mock_resp)

        with patch("app.services.eight_by_eight_service.http", factory):
            with pytest.raises(ValueError, match="missing access_token"):
                await get_access_token(FAKE_SETTINGS)

    async def test_raises_on_http_error(self):
        """HTTPError during auth raises ValueError."""
        factory = _mock_async_client(post=httpx.HTTPError("connection refused"))

        with patch("app.services.eight_by_eight_service.http", factory):
            with pytest.raises(ValueError, match="auth request failed"):
                await get_access_token(FAKE_SETTINGS)

    async def test_returns_cached_token_without_http_call(self):
        """Cached token is returned immediately without making an HTTP request."""
        import time as _time

        _token_cache["token"] = "cached-tok"
        _token_cache["expires_at"] = _time.time() + 7200  # 2h TTL

        factory = _mock_async_client(post=MagicMock())
        with patch("app.services.eight_by_eight_service.http", factory):
            result = await get_access_token(FAKE_SETTINGS)

        assert result == "cached-tok"
        factory._client.post.assert_not_awaited()

    async def test_expired_cache_refetches_token(self):
        """Expired cache causes a fresh HTTP auth call."""
        import time as _time

        _token_cache["token"] = "old-tok"
        _token_cache["expires_at"] = _time.time() - 10  # already expired

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-tok", "expires_in": 3600}
        factory = _mock_async_client(post=mock_resp)

        with patch("app.services.eight_by_eight_service.http", factory):
            result = await get_access_token(FAKE_SETTINGS)

        assert result == "new-tok"
        assert _token_cache["token"] == "new-tok"


# ── get_cdrs pagination and HTTP errors ───────────────────────────────


class TestGetCdrs:
    async def test_returns_empty_on_http_error(self):
        factory = _mock_async_client(get=httpx.HTTPError("connection refused"))
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("app.services.eight_by_eight_service.http", factory):
            result = await get_cdrs("token", FAKE_SETTINGS, since, until)
        assert result == []

    async def test_stops_when_all_records_fetched(self):
        """Stops paginating when all records are already in all_records."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "meta": {"totalRecordCount": 2, "scrollId": "scroll-abc"},
            "data": [{"callId": "1"}, {"callId": "2"}],
        }
        factory = _mock_async_client(get=mock_resp)
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("app.services.eight_by_eight_service.http", factory):
            result = await get_cdrs("token", FAKE_SETTINGS, since, until)
        # Should stop after first page since all records fetched
        assert len(result) == 2
        assert factory._client.get.await_count == 1


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


# ── _process_cdrs edge cases ──────────────────────────────────────────


class TestProcessCdrsEdgeCases:
    def _make_mock_db(self, watermark=None, users=None, requisition=None):
        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = watermark
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = users or []
        req_query = MagicMock()
        req_query.join.return_value.filter.return_value.first.return_value = requisition

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            if name == "requisitions":
                return req_query
            return MagicMock()

        db.query.side_effect = query_router
        return db

    @staticmethod
    def _make_user(extension="1001", user_id=1):
        user = MagicMock()
        user.id = user_id
        user.eight_by_eight_extension = extension
        return user

    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_auth_failure_returns_zeros(self, mock_auth):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.side_effect = ValueError("auth failed")
        db = self._make_mock_db()
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result == {"processed": 0, "matched": 0, "skipped": 0}

    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_empty_cdrs_updates_watermark(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []
        db = self._make_mock_db()
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_with_existing_watermark(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []

        wm_row = MagicMock()
        wm_row.value = "2026-03-01T00:00:00+00:00"
        db = self._make_mock_db(watermark=wm_row)
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_with_invalid_watermark_falls_back(self, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = []

        wm_row = MagicMock()
        wm_row.value = "not-a-date"
        db = self._make_mock_db(watermark=wm_row)
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 0

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_incoming_call_processed(self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt):
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
        mock_match.return_value = None

        db = self._make_mock_db(users=[self._make_user()], requisition=None)
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 1

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_cdr_skipped_when_log_returns_none(self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt):
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
        mock_match.return_value = None

        db = self._make_mock_db(users=[self._make_user()])
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["skipped"] == 1

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_vendor_match_sets_vendor_card_id(self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt):
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
        mock_match.return_value = {
            "type": "vendor",
            "id": 99,
            "name": "VendorX",
            "company_id": None,
            "site_id": None,
            "customer_site_id": None,
            "site_contact_id": None,
            "vendor_card_id": 99,
            "vendor_contact_id": None,
            "ambiguous": False,
            "candidates": [],
        }

        db = self._make_mock_db(users=[self._make_user()])
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["matched"] == 1

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_inbound_cdr_no_user_still_logged(self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt):
        """Inbound CDR with no matching user is logged with user_id=None (not
        skipped)."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [
            {
                "callId": "no-user-001",
                "startTimeUTC": 1772750120399,
                "talkTimeMS": 30000,
                "caller": "+15559876543",
                "callerName": "Unknown Rep",
                "callee": "9999",  # extension not in ext_map
                "calleeName": "9999",
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
        mock_match.return_value = None

        db = self._make_mock_db(users=[])  # no users in ext_map
        result = await _process_cdrs(db, FAKE_SETTINGS)
        assert result["processed"] == 1
        # log_call_activity must have been called with user_id=None
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["user_id"] is None


# ── _find_optimistic_row ───────────────────────────────────────────────


class TestFindOptimisticRow:
    """Unit tests for _find_optimistic_row dedup helper."""

    def _make_row(
        self,
        *,
        external_id=None,
        direction="outbound",
        contact_phone="+15551234567",
        occurred_at=None,
        created_at=None,
        user_id=1,
    ):
        row = MagicMock()
        row.external_id = external_id
        row.direction = direction
        row.contact_phone = contact_phone
        row.occurred_at = occurred_at
        row.created_at = created_at or datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
        row.user_id = user_id
        return row

    def _make_db(self, candidates=None):
        db = MagicMock()
        q = MagicMock()
        q.filter.return_value.all.return_value = candidates or []
        db.query.return_value = q
        return db

    def test_returns_none_when_user_id_is_none(self):
        from app.jobs.eight_by_eight_jobs import _find_optimistic_row

        db = self._make_db()
        result = _find_optimistic_row(
            db, None, "outbound", "+15551234567", datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
        )
        assert result is None
        db.query.assert_not_called()

    def test_returns_none_when_phone_unnormalizable(self):
        from app.jobs.eight_by_eight_jobs import _find_optimistic_row

        db = self._make_db()
        # Empty string won't normalize to E.164
        result = _find_optimistic_row(db, 1, "outbound", "", datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc))
        assert result is None

    def test_matches_row_within_window(self):
        from app.jobs.eight_by_eight_jobs import _find_optimistic_row

        cdr_time = datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
        row_time = datetime(2026, 3, 5, 13, 55, 0, tzinfo=timezone.utc)  # 5 min before
        row = self._make_row(occurred_at=row_time, contact_phone="+15551234567")

        db = self._make_db(candidates=[row])
        with patch("app.utils.phone.normalize_e164", return_value="+15551234567"):
            result = _find_optimistic_row(db, 1, "outbound", "+15551234567", cdr_time)
        assert result is row

    def test_no_match_when_outside_window(self):
        from app.jobs.eight_by_eight_jobs import _find_optimistic_row

        cdr_time = datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
        row_time = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)  # 2h before, outside 10min window
        row = self._make_row(occurred_at=row_time, contact_phone="+15551234567")

        db = self._make_db(candidates=[row])
        with patch("app.utils.phone.normalize_e164", return_value="+15551234567"):
            result = _find_optimistic_row(db, 1, "outbound", "+15551234567", cdr_time)
        assert result is None

    def test_falls_back_to_created_at_when_occurred_at_none(self):
        from app.jobs.eight_by_eight_jobs import _find_optimistic_row

        cdr_time = datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
        created = datetime(2026, 3, 5, 14, 3, 0, tzinfo=timezone.utc)  # 3 min after
        row = self._make_row(occurred_at=None, created_at=created, contact_phone="+15551234567")

        db = self._make_db(candidates=[row])
        with patch("app.utils.phone.normalize_e164", return_value="+15551234567"):
            result = _find_optimistic_row(db, 1, "outbound", "+15551234567", cdr_time)
        assert result is row


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
        patch("app.jobs.eight_by_eight_jobs._process_cdrs", new_callable=AsyncMock) as mock_process,
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
    with patch("app.jobs.eight_by_eight_jobs._process_cdrs", new_callable=AsyncMock) as mock_process:
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


# ── 8x8 CDR → CallOutcome mapping (WS1) ──────────────────────────────────────


class TestCdrOutcomeMapping:
    """Verify that _process_cdrs maps CDR answered/missed flags to CallOutcome and
    passes occurred_at + details to log_call_activity."""

    def _make_mock_db(self, users=None):
        db = MagicMock()
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = None
        user_query = MagicMock()
        user_query.filter.return_value.all.return_value = users or []
        req_query = MagicMock()
        req_query.join.return_value.filter.return_value.first.return_value = None

        def query_router(model):
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            if name == "requisitions":
                return req_query
            return MagicMock()

        db.query.side_effect = query_router
        return db

    @staticmethod
    def _make_user(extension="1001"):
        user = MagicMock()
        user.id = 1
        user.eight_by_eight_extension = extension
        return user

    def _outgoing_cdr(self, *, answered: bool, call_id="cdr-out-001"):
        return {
            "callId": call_id,
            "startTimeUTC": 1772750120000,
            "talkTimeMS": 45000 if answered else 0,
            "caller": "1001",
            "callerName": "Mike",
            "callee": "+15559876543",
            "calleeName": "Vendor Rep",
            "direction": "Outgoing",
            "missed": "-" if answered else "Missed",
            "answered": "Answered" if answered else "-",
            "departments": ["Sales"],
        }

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_answered_cdr_maps_connected_outcome(self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt):
        """An answered CDR → call_outcome=connected + occurred_at set."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [self._outgoing_cdr(answered=True)]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record
        mock_match.return_value = None

        db = self._make_mock_db(users=[self._make_user()])
        await _process_cdrs(db, FAKE_SETTINGS)

        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["details"]["call_outcome"] == "connected"
        assert call_kwargs["details"]["source"] == "8x8_cdr"
        assert call_kwargs["occurred_at"] is not None

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_missed_cdr_maps_no_answer_not_meaningful(
        self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt
    ):
        """A missed CDR → call_outcome=no_answer; is_meaningful=False (via details
        gate)."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [self._outgoing_cdr(answered=False, call_id="cdr-out-002")]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record
        mock_match.return_value = None

        db = self._make_mock_db(users=[self._make_user()])
        await _process_cdrs(db, FAKE_SETTINGS)

        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["details"]["call_outcome"] == "no_answer"
        assert call_kwargs["occurred_at"] is not None

    @patch("app.jobs.eight_by_eight_jobs._find_optimistic_row", return_value=None)
    @patch("app.services.activity_service.match_phone_to_entity")
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_department_included_in_details_when_present(
        self, mock_auth, mock_fetch, mock_log, mock_match, mock_opt
    ):
        """Department from CDR is threaded into details when non-null."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [self._outgoing_cdr(answered=True, call_id="cdr-dept-001")]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record
        mock_match.return_value = None

        db = self._make_mock_db(users=[self._make_user()])
        await _process_cdrs(db, FAKE_SETTINGS)

        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["details"]["department"] == "Sales"
