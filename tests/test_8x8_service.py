"""Tests for 8x8 Work Analytics API client service."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.eight_by_eight_service import get_access_token, get_cdrs, normalize_cdr

FAKE_SETTINGS = SimpleNamespace(
    eight_by_eight_api_key="test-key",
    eight_by_eight_username="user@test.com",
    eight_by_eight_password="secret",
    eight_by_eight_pbx_id="pbx-123",
    eight_by_eight_timezone="America/Los_Angeles",
)


class TestGetAccessToken:
    @patch("app.services.eight_by_eight_service.httpx.post")
    def test_returns_token_on_200(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "tok-abc123"}
        mock_post.return_value = mock_resp

        token = get_access_token(FAKE_SETTINGS)
        assert token == "tok-abc123"
        mock_post.assert_called_once()

    @patch("app.services.eight_by_eight_service.httpx.post")
    def test_raises_on_401(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_post.return_value = mock_resp

        with pytest.raises(ValueError, match="HTTP 401"):
            get_access_token(FAKE_SETTINGS)


class TestGetCdrs:
    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_empty_on_api_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_get.return_value = mock_resp

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        assert result == []

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_returns_records_from_data_key(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "meta": {"totalRecordCount": 2},
            "data": [
                {"callId": "1", "direction": "Incoming", "caller": "+15551234567"},
                {"callId": "2", "direction": "Outgoing", "caller": "1003"},
            ],
        }
        mock_get.return_value = mock_resp

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        assert len(result) == 2

    @patch("app.services.eight_by_eight_service.httpx.get")
    def test_paginates_via_scroll_id(self, mock_get):
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
        mock_get.side_effect = [page1, page2]

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        assert len(result) == 3
        assert mock_get.call_count == 2


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
