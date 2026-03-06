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
    def test_filters_internal_calls(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"callId": "1", "direction": "Inbound", "caller": "+15551234567"},
                {"callId": "2", "direction": "Internal", "caller": "ext100"},
                {"callId": "3", "direction": "Outbound", "caller": "+15559876543"},
            ]
        }
        mock_get.return_value = mock_resp

        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        until = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = get_cdrs("token", FAKE_SETTINGS, since, until)
        assert len(result) == 2
        assert all(r["direction"] != "Internal" for r in result)


class TestNormalizeCdr:
    def test_maps_talk_time_to_duration(self):
        cdr = {
            "callId": "call-1",
            "startTime": "2026-03-05T14:30:00Z",
            "talkTimeMS": 45000,
            "caller": "+15551234567",
            "callee": "+15559876543",
            "callerName": "John Doe",
            "calleeName": "Jane Smith",
            "direction": "Outbound",
            "missed": "No",
        }
        result = normalize_cdr(cdr)
        assert result["external_id"] == "call-1"
        assert result["duration_seconds"] == 45
        assert result["caller_phone"] == "+15551234567"
        assert result["callee_phone"] == "+15559876543"
        assert result["caller_name"] == "John Doe"
        assert result["callee_name"] == "Jane Smith"
        assert result["direction"] == "Outbound"
        assert result["is_missed"] is False
        assert result["occurred_at"].year == 2026

    def test_handles_missing_fields(self):
        result = normalize_cdr({})
        assert result["external_id"] == ""
        assert result["duration_seconds"] == 0
        assert result["caller_phone"] == ""
        assert result["callee_phone"] == ""
        assert result["is_missed"] is False
        assert isinstance(result["occurred_at"], datetime)
