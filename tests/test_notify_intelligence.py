"""Tests for notification intelligence service (Phase 1).

Tests priority classification, staleness detection, engagement tracking,
intelligence gating, and batch digest functionality.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, Time, Boolean
from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from tests.conftest import engine
from app.models.base import Base
from app.models.notification_engagement import NotificationEngagement


# ── Model Tests ──────────────────────────────────────────────────────


class TestNotificationEngagementModel:
    def test_model_columns(self):
        """Verify NotificationEngagement has all expected columns."""
        cols = {c.name for c in NotificationEngagement.__table__.columns}
        expected = {
            "id", "user_id", "event_type", "entity_id", "delivery_method",
            "action", "response_time_s", "ai_priority", "ai_confidence",
            "suppression_reason", "created_at",
        }
        assert expected.issubset(cols)

    def test_tablename(self):
        assert NotificationEngagement.__tablename__ == "notification_engagement"


class TestTeamsAlertConfigExtensions:
    def test_new_columns_exist(self):
        from app.models.teams_alert_config import TeamsAlertConfig
        cols = {c.name for c in TeamsAlertConfig.__table__.columns}
        assert "priority_threshold" in cols
        assert "batch_digest_enabled" in cols
        assert "quiet_hours_start" in cols
        assert "quiet_hours_end" in cols


class TestTeamsNotificationLogExtensions:
    def test_new_columns_exist(self):
        from app.models.teams_notification_log import TeamsNotificationLog
        cols = {c.name for c in TeamsNotificationLog.__table__.columns}
        assert "user_id" in cols
        assert "ai_priority" in cols
        assert "ai_decision" in cols
        assert "batch_id" in cols


# ── Priority Classification ──────────────────────────────────────────


class TestPriorityClassification:
    def test_connector_down_is_critical(self):
        from app.services.notify_intelligence import _classify_priority
        priority, confidence = _classify_priority("connector_down")
        assert priority == "critical"
        assert confidence >= 0.9

    def test_hot_requirement_high_value(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("hot_requirement", {"total_value": 100_000})
        assert priority == "critical"

    def test_hot_requirement_medium_value(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("hot_requirement", {"total_value": 25_000})
        assert priority == "high"

    def test_hot_requirement_low_value(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("hot_requirement", {"total_value": 5_000})
        assert priority == "medium"

    def test_competitive_quote_big_undercut(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("competitive_quote", {"savings_pct": 35})
        assert priority == "critical"

    def test_competitive_quote_normal(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("competitive_quote", {"savings_pct": 22})
        assert priority == "high"

    def test_pipeline_milestone_won(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("pipeline_milestone", {"status": "won"})
        assert priority == "critical"

    def test_pipeline_milestone_lost(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("pipeline_milestone", {"status": "lost"})
        assert priority == "high"

    def test_buyplan_completed_low(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("buyplan_completed")
        assert priority == "low"

    def test_unknown_event_defaults_medium(self):
        from app.services.notify_intelligence import _classify_priority
        priority, _ = _classify_priority("some_unknown_event")
        assert priority == "medium"


# ── AlertDecision ────────────────────────────────────────────────────


class TestAlertDecision:
    def test_dataclass_fields(self):
        from app.services.notify_intelligence import AlertDecision
        d = AlertDecision(action="SEND_NOW", priority="high", confidence=0.95, reason="test")
        assert d.action == "SEND_NOW"
        assert d.priority == "high"
        assert d.confidence == 0.95
        assert d.reason == "test"
        assert d.batch_key == ""


# ── evaluate_channel_alert ──────────────────────────────────────────


class TestEvaluateChannelAlert:
    def test_critical_event_sends_now(self):
        from app.services.notify_intelligence import evaluate_channel_alert
        decision = evaluate_channel_alert("connector_down", "nexar")
        assert decision.action == "SEND_NOW"
        assert decision.priority == "critical"

    def test_low_priority_still_sends_for_channel(self):
        from app.services.notify_intelligence import evaluate_channel_alert
        decision = evaluate_channel_alert("buyplan_completed", "123")
        assert decision.action == "SEND_NOW"  # Channel posts always send

    def test_unknown_event_sends(self):
        from app.services.notify_intelligence import evaluate_channel_alert
        decision = evaluate_channel_alert("totally_new", "99")
        assert decision.action == "SEND_NOW"

    @patch("app.services.notify_intelligence._check_staleness", return_value=True)
    def test_stale_event_suppressed(self, mock_stale):
        from app.services.notify_intelligence import evaluate_channel_alert
        decision = evaluate_channel_alert("hot_requirement", "123")
        assert decision.action == "SUPPRESS"
        assert decision.priority == "noise"


# ── evaluate_dm_alert ──────────────────────────────────────────────


class TestEvaluateDmAlert:
    def test_critical_always_sends(self):
        from app.services.notify_intelligence import evaluate_dm_alert
        decision = evaluate_dm_alert(1, "connector_down", "nexar", "Nexar is down")
        assert decision.action == "SEND_NOW"
        assert decision.priority == "critical"

    def test_low_priority_batched(self):
        from app.services.notify_intelligence import evaluate_dm_alert
        decision = evaluate_dm_alert(1, "buyplan_completed", "123", "Plan complete")
        assert decision.action == "BATCH"
        assert decision.priority == "low"

    @patch("app.services.notify_intelligence._check_staleness", return_value=True)
    def test_stale_suppressed(self, mock_stale):
        from app.services.notify_intelligence import evaluate_dm_alert
        decision = evaluate_dm_alert(1, "hot_requirement", "123", "Hot req")
        assert decision.action == "SUPPRESS"

    def test_error_falls_through(self):
        from app.services.notify_intelligence import evaluate_dm_alert
        with patch("app.services.notify_intelligence._classify_priority", side_effect=RuntimeError("boom")):
            decision = evaluate_dm_alert(1, "test", "1", "msg")
            assert decision.action == "SEND_NOW"  # Fallback


# ── record_engagement ──────────────────────────────────────────────


class TestRecordEngagement:
    def test_records_engagement(self, db_session):
        from app.services.notify_intelligence import record_engagement
        record_engagement(
            user_id=1, event_type="test", entity_id="123",
            action="delivered", ai_priority="high", db=db_session,
        )
        db_session.commit()
        row = db_session.query(NotificationEngagement).filter(
            NotificationEngagement.user_id == 1,
            NotificationEngagement.event_type == "test",
        ).first()
        assert row is not None
        assert row.action == "delivered"
        assert row.ai_priority == "high"

    def test_records_suppression(self, db_session):
        from app.services.notify_intelligence import record_engagement
        record_engagement(
            user_id=1, event_type="noise_event", entity_id="456",
            action="suppressed", suppression_reason="stale", db=db_session,
        )
        db_session.commit()
        row = db_session.query(NotificationEngagement).filter(
            NotificationEngagement.action == "suppressed",
        ).first()
        assert row is not None
        assert row.suppression_reason == "stale"


# ── is_intelligence_enabled ──────────────────────────────────────────


class TestIsIntelligenceEnabled:
    def test_disabled_by_default(self):
        from app.services.notify_intelligence import is_intelligence_enabled
        with patch.dict(os.environ, {"TESTING": "1"}, clear=False):
            # Remove the enabled flag if present
            os.environ.pop("NOTIFICATION_INTELLIGENCE_ENABLED", None)
            assert is_intelligence_enabled() is False

    def test_enabled_when_set(self):
        from app.services.notify_intelligence import is_intelligence_enabled
        with patch.dict(os.environ, {"TESTING": "1", "NOTIFICATION_INTELLIGENCE_ENABLED": "true"}, clear=False):
            assert is_intelligence_enabled() is True


# ── Batch Queue ──────────────────────────────────────────────────────


class TestBatchQueue:
    def test_queue_and_get_without_redis(self):
        """Without Redis, queue/get should be no-ops."""
        from app.services.notify_intelligence import queue_batch_alert, get_batch_queue
        queue_batch_alert(1, "test", "123", "msg")
        items = get_batch_queue(1)
        assert items == []  # No Redis in testing


# ── Intelligence Gate in teams.py ────────────────────────────────────


class TestIntelligenceGate:
    def test_gate_falls_through_when_disabled(self):
        from app.services.teams import _intelligence_gate
        with patch("app.services.teams._is_rate_limited", return_value=False):
            assert _intelligence_gate("test", "123") is True

    def test_gate_blocks_when_rate_limited(self):
        from app.services.teams import _intelligence_gate
        with patch("app.services.teams._is_rate_limited", return_value=True):
            assert _intelligence_gate("test", "123") is False

    @patch("app.services.notify_intelligence.is_intelligence_enabled", return_value=True)
    @patch("app.services.notify_intelligence.evaluate_channel_alert")
    def test_gate_uses_intelligence_when_enabled(self, mock_eval, mock_enabled):
        from app.services.notify_intelligence import AlertDecision
        mock_eval.return_value = AlertDecision(action="SUPPRESS", priority="noise", reason="stale")

        from app.services.teams import _intelligence_gate
        assert _intelligence_gate("test", "123") is False


# ── Digest Formatting ────────────────────────────────────────────────


class TestDigestFormatting:
    def test_format_digest_groups_by_type(self):
        from app.jobs.notify_intelligence_jobs import _format_digest
        items = [
            {"event_type": "hot_requirement", "message": "Hot req 1"},
            {"event_type": "hot_requirement", "message": "Hot req 2"},
            {"event_type": "price_drop", "message": "Price dropped"},
        ]
        result = _format_digest("John", items)
        assert "ALERT DIGEST (3 batched)" in result
        assert "HOT REQUIREMENT" in result
        assert "PRICE DROP" in result

    def test_format_digest_empty(self):
        from app.jobs.notify_intelligence_jobs import _format_digest
        assert _format_digest("John", []) is None


# ── Fixture ──────────────────────────────────────────────────────────


@pytest.fixture
def db_session():
    """Create an in-memory SQLite session with notification tables."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine("sqlite:///:memory:")
    NotificationEngagement.__table__.create(test_engine, checkfirst=True)
    TestSession = sessionmaker(bind=test_engine)
    session = TestSession()
    yield session
    session.close()
