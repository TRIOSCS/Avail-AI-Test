"""Integration tests for self-heal pipeline lifecycle scenarios.

Covers: low-risk full flow, high-risk escalation, budget exceeded.

Called by: pytest
Depends on: app.services.{trouble_ticket_service, diagnosis_service,
            execution_service, cost_controller, rollback_service,
            notification_service, pattern_tracker}
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.self_heal_log import SelfHealLog
from app.services.cost_controller import check_budget
from app.services.diagnosis_service import diagnose_full
from app.services.execution_service import execute_fix
from app.services.pattern_tracker import get_health_status, get_weekly_stats
from app.services.rollback_service import verify_and_retest
from app.services.trouble_ticket_service import create_ticket, update_ticket


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def integ_user(db_session: Session) -> User:
    user = User(
        email="integ@trioscs.com",
        name="Integration User",
        role="admin",
        azure_id="test-integ-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestLowRiskFullFlow:
    """Low-risk ticket: submit -> diagnose -> execute -> verify -> resolve."""

    @patch("app.services.execution_service._write_fix_queue")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_full_lifecycle(self, mock_claude, mock_gen, mock_write, db_session, integ_user):
        # 1. Submit ticket
        ticket = create_ticket(
            db=db_session,
            user_id=integ_user.id,
            title="Button misaligned",
            description="The submit button is off-center",
            current_page="/rfq",
        )
        assert ticket.status == "submitted"

        # 2. Diagnose — mock Claude returns low-risk triage + diagnosis
        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.9, "summary": "CSS bug"},
            {
                "root_cause": "CSS margin",
                "affected_files": ["app/static/app.css"],
                "fix_approach": "Fix margin",
                "test_strategy": "Visual check",
                "estimated_complexity": "simple",
                "requires_migration": False,
            },
        ]
        result = _run(diagnose_full(ticket.id, db_session))
        db_session.refresh(ticket)
        assert ticket.status == "diagnosed"
        assert ticket.risk_tier == "low"
        assert ticket.diagnosis is not None

        # 3. Execute fix — mock _generate_fix returns success
        mock_gen.return_value = {
            "success": True,
            "patches": [{"file": "app/static/app.css", "diff": "margin fix"}],
            "summary": "Fixed margin",
            "cost_usd": 0.05,
        }
        exec_result = _run(execute_fix(ticket.id, db_session))
        assert exec_result["ok"] is True
        db_session.refresh(ticket)
        assert ticket.status == "fix_queued"

        # 4. Post-fix health check — mock SiteTester to avoid Playwright
        with patch("app.services.site_tester.SiteTester") as MockTester:
            instance = AsyncMock()
            instance.run_full_sweep = AsyncMock(return_value=[])
            MockTester.return_value = instance
            health = _run(verify_and_retest(ticket.id, db_session))
        assert health["ok"] is True
        assert health["status"] == "resolved"

        # 5. Ticket already resolved by verify_and_retest
        db_session.refresh(ticket)
        assert ticket.status == "resolved"

        # 5. Stats should reflect the resolved ticket
        stats = get_weekly_stats(db_session)
        assert stats["tickets_created"] >= 1
        assert stats["tickets_resolved"] >= 1

        # 6. Health should be green (no open tickets)
        health_status = get_health_status(db_session)
        assert health_status["status"] == "green"


class TestHighRiskEscalation:
    """High-risk ticket gets blocked from execution, stays diagnosed."""

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_high_risk_blocked(self, mock_claude, db_session, integ_user):
        # 1. Submit
        ticket = create_ticket(
            db=db_session,
            user_id=integ_user.id,
            title="Database migration needed",
            description="Schema change required",
        )

        # 2. Diagnose — returns high risk
        mock_claude.side_effect = [
            {"category": "database", "risk_tier": "high", "confidence": 0.95, "summary": "Schema migration"},
            {
                "root_cause": "Missing column",
                "affected_files": ["alembic/versions/"],
                "fix_approach": "Add column",
                "test_strategy": "Run migration",
                "estimated_complexity": "complex",
                "requires_migration": True,
            },
        ]
        _run(diagnose_full(ticket.id, db_session))
        db_session.refresh(ticket)
        assert ticket.risk_tier == "high"
        assert ticket.status == "diagnosed"

        # 3. Attempt execute — should be rejected (human intervention required)
        exec_result = _run(execute_fix(ticket.id, db_session))
        assert "error" in exec_result
        assert "human" in exec_result["error"].lower()
        db_session.refresh(ticket)
        # Status unchanged — high-risk tickets stay diagnosed until manual action
        assert ticket.status == "diagnosed"

        # 4. Health status counts only tickets with status in ("open", "in_progress").
        # A "diagnosed" ticket is not counted as open, so health stays green.
        # This is by design: diagnosed tickets have been triaged and are awaiting action.
        health = get_health_status(db_session)
        assert health["status"] == "green"


class TestBudgetExceeded:
    """Ticket should be rejected when budget cap is hit."""

    def test_weekly_budget_exceeded(self, db_session, integ_user):
        ticket = create_ticket(
            db=db_session,
            user_id=integ_user.id,
            title="Budget test",
            description="D",
        )

        # Simulate heavy prior spending exceeding weekly budget ($50 cap)
        # Create SelfHealLog entries with cost_usd directly
        for i in range(30):
            t = create_ticket(db=db_session, user_id=integ_user.id, title=f"Old-{i}", description="D")
            db_session.add(SelfHealLog(ticket_id=t.id, cost_usd=2.0))
        db_session.commit()

        budget = check_budget(db_session, ticket.id)
        assert budget["allowed"] is False
        assert "weekly" in budget["reason"].lower()
