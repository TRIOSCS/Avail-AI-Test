"""Tests for cost controller service.

Covers: check_budget, record_cost, get_ticket_spend, get_weekly_spend,
budget cap enforcement, edge cases.

Called by: pytest
Depends on: app.services.cost_controller, app.models.self_heal_log
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.cost_controller import (
    check_budget,
    get_ticket_spend,
    get_weekly_spend,
    record_cost,
)


@pytest.fixture()
def cost_user(db_session: Session) -> User:
    user = User(
        email="cost@trioscs.com",
        name="Cost User",
        role="admin",
        azure_id="test-cost-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def cost_ticket(db_session: Session, cost_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-20260302-C01",
        submitted_by=cost_user.id,
        title="Cost test ticket",
        description="Testing budget caps",
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _add_log(db, ticket_id, cost=None, created_at=None):
    log = SelfHealLog(
        ticket_id=ticket_id,
        category="api",
        risk_tier="low",
        cost_usd=cost,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()
    return log


class TestGetTicketSpend:
    def test_zero_when_no_logs(self, db_session, cost_ticket):
        assert get_ticket_spend(db_session, cost_ticket.id) == 0.0

    def test_sums_across_logs(self, db_session, cost_ticket):
        _add_log(db_session, cost_ticket.id, cost=0.50)
        _add_log(db_session, cost_ticket.id, cost=0.75)
        assert get_ticket_spend(db_session, cost_ticket.id) == 1.25

    def test_ignores_null_costs(self, db_session, cost_ticket):
        _add_log(db_session, cost_ticket.id, cost=None)
        _add_log(db_session, cost_ticket.id, cost=0.50)
        assert get_ticket_spend(db_session, cost_ticket.id) == 0.50


class TestGetWeeklySpend:
    def test_zero_when_empty(self, db_session):
        assert get_weekly_spend(db_session) == 0.0

    def test_sums_current_week(self, db_session, cost_ticket):
        _add_log(db_session, cost_ticket.id, cost=1.00)
        _add_log(db_session, cost_ticket.id, cost=2.00)
        assert get_weekly_spend(db_session) == 3.00

    def test_excludes_old_weeks(self, db_session, cost_ticket):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        _add_log(db_session, cost_ticket.id, cost=5.00, created_at=old)
        _add_log(db_session, cost_ticket.id, cost=1.00)  # this week
        assert get_weekly_spend(db_session) == 1.00


class TestCheckBudget:
    @patch("app.services.cost_controller.settings")
    def test_allowed_within_budget(self, mock_settings, db_session, cost_ticket):
        mock_settings.self_heal_ticket_budget = 2.00
        mock_settings.self_heal_weekly_budget = 50.00
        result = check_budget(db_session, cost_ticket.id)
        assert result["allowed"] is True
        assert result["ticket_spend"] == 0.0

    @patch("app.services.cost_controller.settings")
    def test_denied_ticket_over_budget(self, mock_settings, db_session, cost_ticket):
        mock_settings.self_heal_ticket_budget = 1.00
        mock_settings.self_heal_weekly_budget = 50.00
        _add_log(db_session, cost_ticket.id, cost=1.50)
        result = check_budget(db_session, cost_ticket.id)
        assert result["allowed"] is False
        assert "budget exceeded" in result["reason"]

    @patch("app.services.cost_controller.settings")
    def test_denied_weekly_over_budget(self, mock_settings, db_session, cost_ticket):
        mock_settings.self_heal_ticket_budget = 10.00
        mock_settings.self_heal_weekly_budget = 2.00
        _add_log(db_session, cost_ticket.id, cost=3.00)
        result = check_budget(db_session, cost_ticket.id)
        assert result["allowed"] is False
        assert "Weekly budget" in result["reason"]

    @patch("app.services.cost_controller.settings")
    def test_exact_limit_denied(self, mock_settings, db_session, cost_ticket):
        mock_settings.self_heal_ticket_budget = 1.00
        mock_settings.self_heal_weekly_budget = 50.00
        _add_log(db_session, cost_ticket.id, cost=1.00)
        result = check_budget(db_session, cost_ticket.id)
        assert result["allowed"] is False


class TestRecordCost:
    def test_adds_to_existing_log(self, db_session, cost_ticket):
        log = _add_log(db_session, cost_ticket.id, cost=0.50)
        record_cost(db_session, cost_ticket.id, 0.25)
        db_session.refresh(log)
        assert log.cost_usd == 0.75

    def test_adds_to_null_cost(self, db_session, cost_ticket):
        log = _add_log(db_session, cost_ticket.id, cost=None)
        record_cost(db_session, cost_ticket.id, 0.30)
        db_session.refresh(log)
        assert log.cost_usd == 0.30

    def test_uses_most_recent_log(self, db_session, cost_ticket):
        _add_log(db_session, cost_ticket.id, cost=0.10)
        log2 = _add_log(db_session, cost_ticket.id, cost=0.20)
        record_cost(db_session, cost_ticket.id, 0.50)
        db_session.refresh(log2)
        assert log2.cost_usd == 0.70

    def test_no_log_no_error(self, db_session, cost_ticket):
        # Should not raise if no log exists
        record_cost(db_session, cost_ticket.id, 0.50)
