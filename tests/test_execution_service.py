"""Tests for execution service.

Covers: execute_fix pipeline, budget checks, file locks, max iterations,
escalation, notification emission, patch generation mocking.

Called by: pytest
Depends on: app.services.execution_service
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.notification import Notification
from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.execution_service import execute_fix


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def exec_user(db_session: Session) -> User:
    user = User(
        email="exec@trioscs.com",
        name="Exec User",
        role="admin",
        azure_id="test-exec-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def diagnosed_ticket(db_session: Session, exec_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-20260302-E01",
        submitted_by=exec_user.id,
        title="API returns 500",
        description="Vendor endpoint crashes",
        status="diagnosed",
        risk_tier="low",
        category="api",
        diagnosis={"root_cause": "Query error", "fix_approach": "Fix SQL", "test_strategy": "Test endpoint"},
        generated_prompt="Fix the vendor endpoint query error.",
        file_mapping=["app/routers/vendors.py"],
    )
    db_session.add(ticket)
    db_session.commit()
    # Add a SelfHealLog so cost recording works
    log = SelfHealLog(
        ticket_id=ticket.id,
        category="api",
        risk_tier="low",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


class TestExecuteFixValidation:
    def test_ticket_not_found(self, db_session):
        result = _run(execute_fix(99999, db_session))
        assert result == {"error": "Ticket not found"}

    def test_not_diagnosed(self, db_session, exec_user):
        ticket = TroubleTicket(
            ticket_number="TT-E02",
            submitted_by=exec_user.id,
            title="T",
            description="D",
            status="submitted",
        )
        db_session.add(ticket)
        db_session.commit()
        result = _run(execute_fix(ticket.id, db_session))
        assert result["error"] == "Ticket not yet diagnosed"

    def test_wrong_status(self, db_session, exec_user):
        ticket = TroubleTicket(
            ticket_number="TT-E03",
            submitted_by=exec_user.id,
            title="T",
            description="D",
            status="resolved",
            diagnosis={"root_cause": "Done"},
        )
        db_session.add(ticket)
        db_session.commit()
        result = _run(execute_fix(ticket.id, db_session))
        assert "cannot be executed" in result["error"]

    def test_high_risk_rejected(self, db_session, exec_user):
        ticket = TroubleTicket(
            ticket_number="TT-E04",
            submitted_by=exec_user.id,
            title="T",
            description="D",
            status="diagnosed",
            risk_tier="high",
            diagnosis={"root_cause": "Critical"},
        )
        db_session.add(ticket)
        db_session.commit()
        result = _run(execute_fix(ticket.id, db_session))
        assert result["error"] == "High-risk tickets require human intervention"


class TestExecuteFixBudget:
    @patch("app.services.execution_service.check_budget")
    def test_budget_exceeded_escalates(self, mock_budget, db_session, diagnosed_ticket):
        mock_budget.return_value = {
            "allowed": False,
            "reason": "Weekly budget exceeded",
            "ticket_spend": 0.0,
            "weekly_spend": 55.0,
        }
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "Weekly budget exceeded" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "escalated"


class TestExecuteFixFileLock:
    def test_file_lock_conflict(self, db_session, exec_user, diagnosed_ticket):
        # Create another ticket that's fixing the same file.
        # check_file_lock() filters on status == "in_progress".
        other = TroubleTicket(
            ticket_number="TT-E05",
            submitted_by=exec_user.id,
            title="Other fix",
            description="D",
            status="in_progress",
            file_mapping=["app/routers/vendors.py"],
            diagnosis={"root_cause": "Other"},
        )
        db_session.add(other)
        db_session.commit()
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "File lock conflict" in result["error"]


class TestExecuteFixMaxIterations:
    @patch("app.services.execution_service.settings")
    def test_max_iterations_escalates(self, mock_settings, db_session, diagnosed_ticket):
        mock_settings.self_heal_max_iterations_low = 3
        mock_settings.self_heal_max_iterations_medium = 10
        mock_settings.self_heal_ticket_budget = 100.0
        mock_settings.self_heal_weekly_budget = 500.0
        diagnosed_ticket.iterations_used = 3
        db_session.commit()
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "Max iterations" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "escalated"


class TestExecuteFixSuccess:
    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_successful_fix_writes_queue_file(self, mock_gen, db_session, diagnosed_ticket, tmp_path):
        mock_gen.return_value = {
            "patches": [
                {"file": "app/routers/vendors.py", "search": "old", "replace": "new", "explanation": "fix"}
            ],
            "summary": "Fixed the query",
        }
        with patch("app.services.execution_service.FIX_QUEUE_DIR", str(tmp_path)):
            result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert result["ok"] is True
        assert result["status"] == "fix_queued"
        # Verify JSON file was written
        queue_file = tmp_path / f"{diagnosed_ticket.id}.json"
        assert queue_file.exists()
        payload = json.loads(queue_file.read_text())
        assert payload["ticket_id"] == diagnosed_ticket.id
        assert len(payload["patches"]) == 1

    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_successful_fix_emits_notification(self, mock_gen, db_session, diagnosed_ticket, tmp_path):
        mock_gen.return_value = {
            "patches": [
                {"file": "app/routers/vendors.py", "search": "old", "replace": "new", "explanation": "fix"}
            ],
            "summary": "Done",
        }
        with patch("app.services.execution_service.FIX_QUEUE_DIR", str(tmp_path)):
            _run(execute_fix(diagnosed_ticket.id, db_session))
        notifs = (
            db_session.query(Notification)
            .filter_by(
                ticket_id=diagnosed_ticket.id,
                event_type="fixed",
            )
            .all()
        )
        assert len(notifs) == 1


class TestExecuteFixFailure:
    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_patch_generation_fails(self, mock_gen, db_session, diagnosed_ticket):
        mock_gen.return_value = None
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "Patch generation failed" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "diagnosed"

    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_empty_patches_fails(self, mock_gen, db_session, diagnosed_ticket):
        mock_gen.return_value = {"patches": [], "summary": "Could not determine fix"}
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "No patches generated" in result["error"]

    @patch("app.services.execution_service.settings")
    @patch("app.services.execution_service.generate_patches", new_callable=AsyncMock)
    def test_failed_fix_final_attempt_escalates(self, mock_gen, mock_settings, db_session, diagnosed_ticket):
        mock_settings.self_heal_max_iterations_low = 2
        mock_settings.self_heal_max_iterations_medium = 10
        mock_settings.self_heal_ticket_budget = 100.0
        mock_settings.self_heal_weekly_budget = 500.0
        mock_gen.return_value = None
        diagnosed_ticket.iterations_used = 1
        db_session.commit()
        result = _run(execute_fix(diagnosed_ticket.id, db_session))
        assert "escalated" in result["error"]
        db_session.refresh(diagnosed_ticket)
        assert diagnosed_ticket.status == "escalated"
