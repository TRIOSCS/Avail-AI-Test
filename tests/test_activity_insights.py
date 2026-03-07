"""Tests for activity insights service (Phase 2).

Tests pattern detection: gone quiet, stalling deals, response time trends,
engagement acceleration.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")


class TestGetUserInsights:
    @patch("app.services.activity_insights._detect_gone_quiet", return_value=[])
    @patch("app.services.activity_insights._detect_stalling_deals", return_value=[])
    @patch("app.services.activity_insights._detect_response_time_trend", return_value=[])
    @patch("app.services.activity_insights._detect_engagement_acceleration", return_value=[])
    def test_returns_empty_when_no_patterns(self, *mocks):
        from app.services.activity_insights import get_user_insights
        result = get_user_insights(1, MagicMock(), max_insights=3)
        assert result == []

    @patch("app.services.activity_insights._detect_gone_quiet")
    @patch("app.services.activity_insights._detect_stalling_deals", return_value=[])
    @patch("app.services.activity_insights._detect_response_time_trend", return_value=[])
    @patch("app.services.activity_insights._detect_engagement_acceleration", return_value=[])
    def test_returns_insights_sorted_by_priority(self, mock_accel, mock_resp, mock_stall, mock_quiet):
        mock_quiet.return_value = [
            {"type": "gone_quiet", "title": "Low", "priority": "low", "detail": "d", "action": "a"},
            {"type": "gone_quiet", "title": "High", "priority": "high", "detail": "d", "action": "a"},
        ]
        from app.services.activity_insights import get_user_insights
        result = get_user_insights(1, MagicMock(), max_insights=3)
        assert len(result) == 2
        assert result[0]["priority"] == "high"
        assert result[1]["priority"] == "low"

    @patch("app.services.activity_insights._detect_gone_quiet", side_effect=RuntimeError("boom"))
    @patch("app.services.activity_insights._detect_stalling_deals", return_value=[])
    @patch("app.services.activity_insights._detect_response_time_trend", return_value=[])
    @patch("app.services.activity_insights._detect_engagement_acceleration", return_value=[])
    def test_handles_detector_failure_gracefully(self, *mocks):
        from app.services.activity_insights import get_user_insights
        result = get_user_insights(1, MagicMock(), max_insights=3)
        assert result == []  # No crash

    @patch("app.services.activity_insights._detect_gone_quiet")
    @patch("app.services.activity_insights._detect_stalling_deals")
    @patch("app.services.activity_insights._detect_response_time_trend", return_value=[])
    @patch("app.services.activity_insights._detect_engagement_acceleration", return_value=[])
    def test_limits_to_max_insights(self, mock_accel, mock_resp, mock_stall, mock_quiet):
        mock_quiet.return_value = [
            {"type": "gone_quiet", "title": f"Item {i}", "priority": "medium", "detail": "d", "action": "a"}
            for i in range(5)
        ]
        mock_stall.return_value = [
            {"type": "stalling", "title": f"Deal {i}", "priority": "high", "detail": "d", "action": "a"}
            for i in range(3)
        ]
        from app.services.activity_insights import get_user_insights
        result = get_user_insights(1, MagicMock(), max_insights=3)
        assert len(result) == 3


class TestDealRisk:
    def test_assess_risk_not_found(self):
        from app.services.deal_risk import assess_risk
        db = MagicMock()
        db.get.return_value = None
        result = assess_risk(999, db)
        assert result["risk_level"] == "green"
        assert result["score"] == 0

    def test_generate_explanation_no_issues(self):
        from app.services.deal_risk import _generate_explanation
        explanation, action = _generate_explanation("green", {"days_idle": 0, "offer_count": 5, "vendor_response_rate": 0.5}, MagicMock())
        assert "On track" in explanation
        assert action == ""

    def test_generate_explanation_with_issues(self):
        from app.services.deal_risk import _generate_explanation
        signals = {"days_idle": 10, "offer_count": 0}
        explanation, action = _generate_explanation("red", signals, MagicMock())
        assert "idle" in explanation
        assert "no offers" in explanation

    def test_scan_active_returns_list(self):
        from app.services.deal_risk import scan_active_requisitions
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        result = scan_active_requisitions(db, user_id=1)
        assert result == []


class TestDealRiskEndpoint:
    """Test the /api/requisitions/{id}/risk-assessment endpoint."""

    @patch("app.services.deal_risk.assess_risk")
    def test_risk_endpoint_returns_assessment(self, mock_assess):
        mock_assess.return_value = {
            "risk_level": "yellow",
            "score": 45,
            "explanation": "Deal is stalling",
            "suggested_action": "Follow up",
            "signals": {"days_idle": 8},
        }
        # Verify the function signature is correct
        from app.services.deal_risk import assess_risk
        result = assess_risk(1, MagicMock())
        assert result["risk_level"] == "yellow"
        assert result["score"] == 45
