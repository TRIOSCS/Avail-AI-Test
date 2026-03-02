"""Tests for the diagnosis service.

Covers: classification, risk overrides, detailed diagnosis, full pipeline.
All Claude calls are mocked.

Called by: pytest
Depends on: app.services.diagnosis_service, conftest fixtures
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.notification import Notification
from app.models.trouble_ticket import TroubleTicket
from app.models.self_heal_log import SelfHealLog
from app.services.trouble_ticket_service import create_ticket
from app.services.diagnosis_service import (
    classify_ticket,
    apply_risk_overrides,
    diagnose_ticket,
    diagnose_full,
)


def _run(coro):
    """Run async coroutine in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_ticket(db, user, **overrides):
    """Create a test ticket."""
    defaults = dict(title="Button broken", description="Submit button does nothing on RFQ page",
                    current_page="/api/rfq")
    defaults.update(overrides)
    return create_ticket(db=db, user_id=user.id, **defaults)


class TestClassifyTicket:
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_returns_classification(self, mock_claude, db_session, test_user):
        mock_claude.return_value = {
            "category": "ui",
            "risk_tier": "low",
            "confidence": 0.85,
            "summary": "Button click handler missing",
        }
        ticket = _make_ticket(db_session, test_user)
        result = _run(classify_ticket(ticket))
        assert result["category"] == "ui"
        assert result["risk_tier"] == "low"
        assert result["confidence"] == 0.85
        mock_claude.assert_called_once()

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_includes_context_in_prompt(self, mock_claude, db_session, test_user):
        mock_claude.return_value = {"category": "api", "risk_tier": "medium", "confidence": 0.7, "summary": "API error"}
        ticket = _make_ticket(db_session, test_user, current_page="/api/vendors")
        _run(classify_ticket(ticket))
        prompt = mock_claude.call_args[1].get("prompt") or mock_claude.call_args[0][0]
        assert "vendors" in prompt.lower() or "/api/vendors" in prompt

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_returns_none_on_failure(self, mock_claude, db_session, test_user):
        mock_claude.return_value = None
        ticket = _make_ticket(db_session, test_user)
        result = _run(classify_ticket(ticket))
        assert result is None


class TestRiskOverrides:
    def test_low_confidence_bumps_low_to_medium(self):
        classification = {"category": "ui", "risk_tier": "low", "confidence": 0.4}
        result = apply_risk_overrides(classification, [])
        assert result["risk_tier"] == "medium"

    def test_low_confidence_bumps_medium_to_high(self):
        classification = {"category": "api", "risk_tier": "medium", "confidence": 0.3}
        result = apply_risk_overrides(classification, [])
        assert result["risk_tier"] == "high"

    def test_high_confidence_no_change(self):
        classification = {"category": "ui", "risk_tier": "low", "confidence": 0.9}
        result = apply_risk_overrides(classification, [])
        assert result["risk_tier"] == "low"

    def test_stable_files_force_high(self):
        classification = {"category": "api", "risk_tier": "low", "confidence": 0.9}
        files = [{"path": "app/main.py", "role": "mentioned", "stable": True}]
        result = apply_risk_overrides(classification, files)
        assert result["risk_tier"] == "high"

    def test_no_stable_files_no_change(self):
        classification = {"category": "ui", "risk_tier": "low", "confidence": 0.9}
        files = [{"path": "app/routers/foo.py", "role": "router", "stable": False}]
        result = apply_risk_overrides(classification, files)
        assert result["risk_tier"] == "low"


class TestDiagnoseTicket:
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_returns_diagnosis(self, mock_claude, db_session, test_user):
        mock_claude.return_value = {
            "root_cause": "Missing onclick handler",
            "affected_files": ["app/static/app.js"],
            "fix_approach": "Add event listener to submit button",
            "test_strategy": "Test button click triggers form submission",
            "estimated_complexity": "simple",
            "requires_migration": False,
        }
        ticket = _make_ticket(db_session, test_user)
        classification = {"category": "ui", "risk_tier": "low", "confidence": 0.9}
        result = _run(diagnose_ticket(ticket, classification))
        assert result["root_cause"] == "Missing onclick handler"
        assert "app/static/app.js" in result["affected_files"]

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_returns_none_on_failure(self, mock_claude, db_session, test_user):
        mock_claude.return_value = None
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_ticket(ticket, {"category": "ui", "risk_tier": "low"}))
        assert result is None


class TestDiagnoseFull:
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_full_pipeline_low_risk(self, mock_claude, db_session, test_user):
        # First call = classification, second = diagnosis
        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.85, "summary": "UI bug"},
            {"root_cause": "CSS issue", "affected_files": ["app/static/app.js"],
             "fix_approach": "Fix CSS", "test_strategy": "Visual check",
             "estimated_complexity": "simple", "requires_migration": False},
        ]
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_full(ticket.id, db_session))
        assert result["status"] == "diagnosed"
        assert result["risk_tier"] == "low"
        assert result["diagnosis"] is not None
        # Verify ticket was updated
        updated = db_session.get(TroubleTicket, ticket.id)
        assert updated.status == "diagnosed"
        assert updated.risk_tier == "low"
        assert updated.diagnosis is not None

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_high_risk_skips_detailed_diagnosis(self, mock_claude, db_session, test_user):
        mock_claude.return_value = {"category": "data", "risk_tier": "high", "confidence": 0.9, "summary": "Data loss"}
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_full(ticket.id, db_session))
        assert result["risk_tier"] == "high"
        assert result["diagnosis"] is None
        # Only one Claude call (classification, no diagnosis)
        assert mock_claude.call_count == 1

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_migration_forces_high(self, mock_claude, db_session, test_user):
        mock_claude.side_effect = [
            {"category": "data", "risk_tier": "low", "confidence": 0.9, "summary": "Schema issue"},
            {"root_cause": "Missing column", "affected_files": ["app/models/foo.py"],
             "fix_approach": "Add migration", "test_strategy": "Test migration",
             "estimated_complexity": "moderate", "requires_migration": True},
        ]
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_full(ticket.id, db_session))
        assert result["risk_tier"] == "high"

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_complex_low_bumps_to_medium(self, mock_claude, db_session, test_user):
        mock_claude.side_effect = [
            {"category": "api", "risk_tier": "low", "confidence": 0.85, "summary": "API issue"},
            {"root_cause": "Complex refactor needed", "affected_files": ["app/services/foo.py"],
             "fix_approach": "Major refactor", "test_strategy": "Full regression",
             "estimated_complexity": "complex", "requires_migration": False},
        ]
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_full(ticket.id, db_session))
        assert result["risk_tier"] == "medium"

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_classification_failure(self, mock_claude, db_session, test_user):
        mock_claude.return_value = None
        ticket = _make_ticket(db_session, test_user)
        result = _run(diagnose_full(ticket.id, db_session))
        assert "error" in result

    def test_ticket_not_found(self, db_session):
        result = _run(diagnose_full(99999, db_session))
        assert result == {"error": "Ticket not found"}

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_creates_self_heal_log(self, mock_claude, db_session, test_user):
        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.9, "summary": "Bug"},
            {"root_cause": "Fix", "affected_files": [], "fix_approach": "Do it",
             "test_strategy": "Test it", "estimated_complexity": "simple",
             "requires_migration": False},
        ]
        ticket = _make_ticket(db_session, test_user)
        _run(diagnose_full(ticket.id, db_session))
        logs = db_session.query(SelfHealLog).filter_by(ticket_id=ticket.id).all()
        assert len(logs) == 1
        assert logs[0].category == "ui"
        assert logs[0].risk_tier == "low"

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_emits_notification(self, mock_claude, db_session, test_user):
        mock_claude.side_effect = [
            {"category": "api", "risk_tier": "low", "confidence": 0.9, "summary": "API bug"},
            {"root_cause": "Query error", "affected_files": [], "fix_approach": "Fix SQL",
             "test_strategy": "Test it", "estimated_complexity": "simple",
             "requires_migration": False},
        ]
        ticket = _make_ticket(db_session, test_user)
        _run(diagnose_full(ticket.id, db_session))
        notifs = db_session.query(Notification).filter_by(
            user_id=test_user.id, ticket_id=ticket.id,
        ).all()
        assert len(notifs) == 1
        assert notifs[0].event_type == "prompt_ready"
        assert "API bug" in notifs[0].title

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_high_risk_emits_escalated_notification(self, mock_claude, db_session, test_user):
        mock_claude.return_value = {
            "category": "data", "risk_tier": "high", "confidence": 0.95, "summary": "DB issue",
        }
        ticket = _make_ticket(db_session, test_user)
        _run(diagnose_full(ticket.id, db_session))
        notifs = db_session.query(Notification).filter_by(
            user_id=test_user.id, ticket_id=ticket.id,
        ).all()
        assert len(notifs) == 1
        assert notifs[0].event_type == "escalated"
