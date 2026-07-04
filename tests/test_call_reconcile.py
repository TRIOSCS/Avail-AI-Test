"""test_call_reconcile.py — CDR-optimistic row reconciliation tests.

Verifies that _process_cdrs enriches an existing optimistic click-to-call
row (rather than creating a second ActivityLog) when a matching 8x8 CDR
arrives, and that distinct calls are never merged.

Cases tested:
  (a) click-to-call row + matching CDR → exactly ONE row, enriched
  (b) CDR with no matching optimistic row → new row as today
  (c) two distinct calls (different phone, or same phone outside window) → NOT merged
  (d) re-poll of same CDR (external_id already set) → dedup, no second row
  (e) token caching: reuses within expiry, re-auths after expiry
  (f) get_extension_map is gone (no import errors)

Called by: pytest
Depends on: app/jobs/eight_by_eight_jobs.py, app/services/eight_by_eight_service.py
"""

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["TESTING"] = "1"

FAKE_SETTINGS = SimpleNamespace(
    eight_by_eight_api_key="test-key",
    eight_by_eight_username="user@test.com",
    eight_by_eight_password="secret",
    eight_by_eight_pbx_id="pbx-123",
    eight_by_eight_timezone="America/Los_Angeles",
)

# A fixed "now" used across tests
_CDR_TIME = datetime(2026, 3, 5, 14, 30, 0, tzinfo=timezone.utc)

# Use a real E.164 number that phonenumbers can parse (555 numbers are
# reserved/invalid and normalize_e164 returns None for them).
_PHONE = "+12125550100"

_OUTGOING_CDR = {
    "callId": "cdr-abc-001",
    "startTimeUTC": int(_CDR_TIME.timestamp() * 1000),
    "talkTimeMS": 60000,
    "caller": "1001",
    "callerName": "Mike",
    "callee": _PHONE,
    "calleeName": "Vendor Rep",
    "direction": "Outgoing",
    "missed": "-",
    "answered": "Answered",
    "departments": ["Sales"],
}


def _make_optimistic_row(
    *,
    phone=_PHONE,
    direction="outbound",
    user_id=1,
    occurred_at=None,
    created_at=None,
    details=None,
):
    """Build a MagicMock ActivityLog mimicking a click-to-call optimistic row."""
    row = MagicMock()
    row.id = 42
    row.external_id = None  # un-reconciled
    row.contact_phone = phone
    row.direction = direction
    row.user_id = user_id
    row.occurred_at = occurred_at or (_CDR_TIME - timedelta(minutes=5))
    row.created_at = created_at or (_CDR_TIME - timedelta(minutes=5))
    row.duration_seconds = None
    row.is_meaningful = True
    row.details = details or {}
    return row


def _make_mock_db(*, optimistic_rows=None, users=None):
    """Return a MagicMock db whose query router handles all expected models."""
    db = MagicMock()

    wm_q = MagicMock()
    wm_q.filter.return_value.first.return_value = None

    user_q = MagicMock()
    user_q.filter.return_value.all.return_value = users or []

    req_q = MagicMock()
    req_q.join.return_value.filter.return_value.first.return_value = None

    activity_q = MagicMock()
    activity_q.filter.return_value.all.return_value = optimistic_rows or []

    def _router(model):
        name = getattr(model, "__tablename__", "")
        if name == "system_config":
            return wm_q
        if name == "users":
            return user_q
        if name == "requisitions":
            return req_q
        if name == "activity_log":
            return activity_q
        return MagicMock()

    db.query.side_effect = _router
    return db


def _make_user(extension="1001", user_id=1):
    u = MagicMock()
    u.id = user_id
    u.eight_by_eight_extension = extension
    return u


# ─────────────────────────────────────────────────────────────────────────────
# (a) Optimistic row + matching CDR → exactly ONE enriched row, no double-bump
# ─────────────────────────────────────────────────────────────────────────────


class TestReconcileOptimisticRow:
    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_matching_cdr_enriches_optimistic_row_not_creates_new(
        self, mock_auth, mock_fetch, mock_log, mock_match
    ):
        """CDR matching an optimistic click row enriches it; log_call_activity NOT
        called."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]

        optimistic = _make_optimistic_row()
        db = _make_mock_db(
            optimistic_rows=[optimistic],
            users=[_make_user()],
        )

        result = await _process_cdrs(db, FAKE_SETTINGS)

        # One call processed; log_call_activity NOT called (reconciled instead)
        assert result["processed"] == 1
        mock_log.assert_not_called()

        # Optimistic row was enriched in-place
        assert optimistic.external_id == "cdr-abc-001"
        assert optimistic.duration_seconds == 60
        assert optimistic.details["call_outcome"] == "connected"
        assert optimistic.details["source"] == "8x8_cdr"
        assert optimistic.details["department"] == "Sales"
        assert optimistic.is_meaningful is True  # connected is meaningful

    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_cadence_bumped_once_on_reconcile(self, mock_auth, mock_fetch, mock_log, mock_match):
        """When reconciling, bump_clocks_from_activity is called exactly once.

        The enrich path always calls bump (forward-only/idempotent) to correct vendor
        'Log Call' rows that skipped the click-to-call bump path.  For rows already
        bumped at click, the forward-only guard is a no-op.
        """
        from app.jobs.eight_by_eight_jobs import _process_cdrs
        from app.services import cadence_service

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]

        optimistic = _make_optimistic_row()
        db = _make_mock_db(optimistic_rows=[optimistic], users=[_make_user()])

        with patch.object(cadence_service, "bump_clocks_from_activity") as mock_bump:
            await _process_cdrs(db, FAKE_SETTINGS)
            # bump is called exactly once during reconcile (forward-only, idempotent)
            mock_bump.assert_called_once_with(db, optimistic)


# ─────────────────────────────────────────────────────────────────────────────
# (b) CDR with no matching optimistic row → new row via log_call_activity
# ─────────────────────────────────────────────────────────────────────────────


class TestNoOptimisticRow:
    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_cdr_without_optimistic_row_creates_new_row(self, mock_auth, mock_fetch, mock_log, mock_match):
        """When no matching optimistic row, log_call_activity is called as before."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]

        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record

        # No optimistic rows in the DB
        db = _make_mock_db(optimistic_rows=[], users=[_make_user()])

        result = await _process_cdrs(db, FAKE_SETTINGS)

        assert result["processed"] == 1
        mock_log.assert_called_once()
        kw = mock_log.call_args.kwargs
        assert kw["external_id"] == "cdr-abc-001"
        assert kw["details"]["call_outcome"] == "connected"


# ─────────────────────────────────────────────────────────────────────────────
# (c) Two distinct calls are NOT merged
# ─────────────────────────────────────────────────────────────────────────────


class TestDistinctCallsNotMerged:
    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_different_phone_not_merged(self, mock_auth, mock_fetch, mock_log, mock_match):
        """Optimistic row for a DIFFERENT phone is not consumed by this CDR."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]  # callee = +15551234567

        # Optimistic row for a different (but valid) number
        wrong_phone_row = _make_optimistic_row(phone="+14155550100")
        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record

        db = _make_mock_db(optimistic_rows=[wrong_phone_row], users=[_make_user()])
        result = await _process_cdrs(db, FAKE_SETTINGS)

        # log_call_activity called (no reconcile) + distinct row untouched
        assert result["processed"] == 1
        mock_log.assert_called_once()
        assert wrong_phone_row.external_id is None  # not touched

    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_same_phone_outside_window_not_merged(self, mock_auth, mock_fetch, mock_log, mock_match):
        """Same phone but row time is outside the ±10min window — not reconciled."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]  # CDR at _CDR_TIME

        # Optimistic row 2 hours earlier — outside window
        stale_row = _make_optimistic_row(
            phone="+15551234567",
            occurred_at=_CDR_TIME - timedelta(hours=2),
            created_at=_CDR_TIME - timedelta(hours=2),
        )
        record = MagicMock()
        record.company_id = None
        record.vendor_card_id = None
        mock_log.return_value = record

        db = _make_mock_db(optimistic_rows=[stale_row], users=[_make_user()])
        result = await _process_cdrs(db, FAKE_SETTINGS)

        # New row created, stale row not touched
        mock_log.assert_called_once()
        assert stale_row.external_id is None


# ─────────────────────────────────────────────────────────────────────────────
# (d) Re-poll of same CDR (external_id already set) → dedup, no second row
# ─────────────────────────────────────────────────────────────────────────────


class TestRePollDedup:
    @patch("app.services.activity_service.match_phone_to_entity", return_value=None)
    @patch("app.services.activity_service.log_call_activity")
    @patch("app.services.eight_by_eight_service.get_cdrs", new_callable=AsyncMock)
    @patch("app.services.eight_by_eight_service.get_access_token", new_callable=AsyncMock)
    async def test_repoll_returns_none_skips_row(self, mock_auth, mock_fetch, mock_log, mock_match):
        """Re-polling the same CDR callId → log_call_activity returns None (dedup) →
        skipped."""
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [_OUTGOING_CDR]

        # No optimistic rows; log_call_activity returns None (already logged)
        mock_log.return_value = None
        db = _make_mock_db(optimistic_rows=[], users=[_make_user()])

        result = await _process_cdrs(db, FAKE_SETTINGS)

        assert result["skipped"] == 1
        assert result["processed"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (e) Token caching: reuses within expiry, re-auths after expiry
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenCaching:
    def setup_method(self):
        from app.services.eight_by_eight_service import _token_cache

        _token_cache.clear()

    async def test_reuses_token_within_expiry(self):
        """Token cached with future expiry is returned without HTTP call."""
        from app.services.eight_by_eight_service import _token_cache, get_access_token

        _token_cache["token"] = "valid-tok"
        _token_cache["expires_at"] = time.time() + 7200  # 2h TTL

        mock_post = AsyncMock()
        mock_http = MagicMock()
        mock_http.post = mock_post
        with patch("app.services.eight_by_eight_service.http", mock_http):
            result = await get_access_token(FAKE_SETTINGS)

        assert result == "valid-tok"
        mock_post.assert_not_awaited()

    async def test_re_auths_after_expiry(self):
        """Expired cache forces a fresh HTTP auth and updates the cache."""
        from app.services.eight_by_eight_service import _token_cache, get_access_token

        _token_cache["token"] = "old-tok"
        _token_cache["expires_at"] = time.time() - 10  # expired 10s ago

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "fresh-tok", "expires_in": 3600}

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        with patch("app.services.eight_by_eight_service.http", mock_http):
            result = await get_access_token(FAKE_SETTINGS)

        assert result == "fresh-tok"
        assert _token_cache["token"] == "fresh-tok"
        assert _token_cache["expires_at"] > time.time()

    async def test_expires_in_stored_in_cache(self):
        """expires_in from auth response sets the cache TTL."""
        from app.services.eight_by_eight_service import _token_cache, get_access_token

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "ttl-tok", "expires_in": 1800}

        t_before = time.time()
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        with patch("app.services.eight_by_eight_service.http", mock_http):
            await get_access_token(FAKE_SETTINGS)

        # expires_at should be approximately t_before + 1800
        assert _token_cache["expires_at"] >= t_before + 1799


# ─────────────────────────────────────────────────────────────────────────────
# (f) get_extension_map is gone — no import errors, no public symbol
# ─────────────────────────────────────────────────────────────────────────────


def test_get_extension_map_removed():
    """get_extension_map must not exist in eight_by_eight_service."""
    import app.services.eight_by_eight_service as svc

    assert not hasattr(svc, "get_extension_map"), (
        "get_extension_map still exported from eight_by_eight_service — "
        "it was unused dead code; it should have been deleted."
    )


def test_eight_by_eight_service_importable_without_get_extension_map():
    """The service module imports cleanly and exports the expected public API."""
    from app.services.eight_by_eight_service import (  # noqa: F401
        _token_cache,
        get_access_token,
        get_cdrs,
        normalize_cdr,
        normalize_phone,
    )
