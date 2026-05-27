"""Tests for 8x8 Work Analytics API client service."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.eight_by_eight_service import get_access_token, get_cdrs, normalize_cdr

FAKE_SETTINGS = SimpleNamespace(
    eight_by_eight_api_key="test-key",
    eight_by_eight_username="user@test.com",
    eight_by_eight_password="secret",
    eight_by_eight_pbx_id="pbx-123",
    eight_by_eight_timezone="America/Los_Angeles",
)


def _mock_async_client(*, get=None, post=None):
    """Build a patch target replacing httpx.AsyncClient.

    `get`/`post` may be a mock, a single response, an iterable of responses
    (side_effect), or an exception. Returns a MagicMock suitable as the
    return_value of a patch on `app.services.eight_by_eight_service.httpx.AsyncClient`.
    """
    client = MagicMock()

    def _async_method(spec):
        m = AsyncMock()
        if spec is None:
            return m
        if isinstance(spec, AsyncMock):
            return spec
        if isinstance(spec, BaseException) or (isinstance(spec, type) and issubclass(spec, BaseException)):
            m.side_effect = spec
        elif isinstance(spec, (list, tuple)):
            m.side_effect = list(spec)
        else:
            m.return_value = spec
        return m

    client.get = _async_method(get)
    client.post = _async_method(post)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    factory._client = client  # expose for assertions
    return factory


class TestGetAccessToken:
    async def test_returns_token_on_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "tok-abc123"}
        factory = _mock_async_client(post=mock_resp)

        with patch("app.services.eight_by_eight_service.httpx.AsyncClient", factory):
            token = await get_access_token(FAKE_SETTINGS)
        assert token == "tok-abc123"
        factory._client.post.assert_awaited_once()

    async def test_raises_on_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        factory = _mock_async_client(post=mock_resp)

        with patch("app.services.eight_by_eight_service.httpx.AsyncClient", factory):
            with pytest.raises(ValueError, match="HTTP 401"):
                await get_access_token(FAKE_SETTINGS)


class TestGetCdrs:
    async def test_returns_empty_on_api_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        factory = _mock_async_client(get=mock_resp)

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("app.services.eight_by_eight_service.httpx.AsyncClient", factory):
            result = await get_cdrs("token", FAKE_SETTINGS, since, until)
        assert result == []

    async def test_returns_records_from_data_key(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "meta": {"totalRecordCount": 2},
            "data": [
                {"callId": "1", "direction": "Incoming", "caller": "+15551234567"},
                {"callId": "2", "direction": "Outgoing", "caller": "1003"},
            ],
        }
        factory = _mock_async_client(get=mock_resp)

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("app.services.eight_by_eight_service.httpx.AsyncClient", factory):
            result = await get_cdrs("token", FAKE_SETTINGS, since, until)
        assert len(result) == 2

    async def test_paginates_via_scroll_id(self):
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "meta": {"totalRecordCount": 3, "scrollId": "scroll-1"},
            "data": [{"callId": "1"}, {"callId": "2"}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "meta": {"totalRecordCount": 3},
            "data": [{"callId": "3"}],
        }
        factory = _mock_async_client(get=[page1, page2])

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("app.services.eight_by_eight_service.httpx.AsyncClient", factory):
            result = await get_cdrs("token", FAKE_SETTINGS, since, until)
        assert len(result) == 3
        assert factory._client.get.await_count == 2


class TestNormalizeCdr:
    def test_maps_real_8x8_record(self):
        """Test with a record matching real 8x8 API output."""
        cdr = {
            "callId": "1761887844732",
            "startTimeUTC": 1772750120399,
            "startTime": "2026-03-05T14:35:20.399-0800",
            "talkTimeMS": 748421,
            "caller": "1003",
            "callerName": "Katy Cienfuegos",
            "callee": "+17149331488",
            "calleeName": "",
            "direction": "Outgoing",
            "missed": "-",
            "answered": "Answered",
            "departments": ["Accounting"],
        }
        result = normalize_cdr(cdr)
        assert result["external_id"] == "1761887844732"
        assert result["duration_seconds"] == 748
        assert result["caller_phone"] == "1003"
        assert result["callee_phone"] == "+17149331488"
        assert result["caller_name"] == "Katy Cienfuegos"
        assert result["direction"] == "Outgoing"
        assert result["is_missed"] is False
        assert result["is_answered"] is True
        assert result["extension"] == "1003"
        assert result["department"] == "Accounting"
        assert result["occurred_at"].year == 2026

    def test_incoming_missed_call(self):
        cdr = {
            "callId": "1761887844737",
            "startTimeUTC": 1772757793502,
            "talkTimeMS": 0,
            "caller": "+17142630481",
            "callerName": ".",
            "callee": "1021",
            "calleeName": "Main AA Trio Supply Chain Solutions",
            "direction": "Incoming",
            "missed": "Missed",
            "answered": "-",
            "departments": None,
        }
        result = normalize_cdr(cdr)
        assert result["is_missed"] is True
        assert result["is_answered"] is False
        assert result["duration_seconds"] == 0
        assert result["extension"] == "1021"
        assert result["department"] is None

    def test_handles_missing_fields(self):
        result = normalize_cdr({})
        assert result["external_id"] == ""
        assert result["duration_seconds"] == 0
        assert result["caller_phone"] == ""
        assert result["callee_phone"] == ""
        assert result["is_missed"] is False
        assert result["is_answered"] is False
        assert result["extension"] == ""
        assert result["department"] is None
        assert isinstance(result["occurred_at"], datetime)
