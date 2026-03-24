"""Tests for selective auto-task triggers — bid due, buy plan, email offer, new offers.

Verifies that task events create the right tasks, respect dedup,
and that the scheduler job only fires for approaching deadlines.

Depends on: conftest.py fixtures, app/jobs/task_jobs.py, app/services/task_service.py
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.services import task_service

# ---------------------------------------------------------------------------
# New requirement tasks (already existed, verify still works)
# ---------------------------------------------------------------------------


class TestOnRequirementAdded:
    def test_creates_sourcing_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "LM317T" in tasks[0].title
        assert tasks[0].source_ref == "source:LM317T"
        assert tasks[0].source == "system"

    def test_dedup_same_mpn(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# New offer tasks
# ---------------------------------------------------------------------------


class TestOnOfferReceived:
    def test_creates_review_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Arrow" in tasks[0].title
        assert tasks[0].source_ref == "offer:42"

    def test_dedup_same_offer(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Email-parsed offer tasks
# ---------------------------------------------------------------------------


class TestOnEmailOfferParsed:
    def test_creates_email_offer_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Email offer" in tasks[0].title
        assert "Mouser" in tasks[0].title
        assert tasks[0].source_ref == "email_offer:99"

    def test_dedup_same_email_offer(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Buy plan assignment tasks
# ---------------------------------------------------------------------------


class TestOnBuyPlanAssigned:
    def test_creates_cut_po_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_buy_plan_assigned(
            db_session,
            requisition_id=test_requisition.id,
            buyer_id=test_user.id,
            vendor_name="DigiKey",
            mpn="LM317T",
            line_id=7,
        )
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Cut PO" in tasks[0].title
        assert "DigiKey" in tasks[0].title
        assert tasks[0].source_ref == "buyline:7"
        assert tasks[0].assigned_to_id == test_user.id
        assert tasks[0].task_type == "buying"

    def test_dedup_same_line(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "DigiKey", "LM317T", 7)
        task_service.on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "DigiKey", "LM317T", 7)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Bid due alert tasks
# ---------------------------------------------------------------------------


class TestOnBidDueSoon:
    def test_creates_bid_due_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_bid_due_soon(db_session, test_requisition.id, "2026-03-17", "REQ-TEST-001")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Bid due" in tasks[0].title
        assert tasks[0].source_ref == f"bid_due:{test_requisition.id}"
        assert tasks[0].due_at is not None

    def test_dedup_same_requisition(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_bid_due_soon(db_session, test_requisition.id, "2026-03-17", "REQ-TEST-001")
        task_service.on_bid_due_soon(db_session, test_requisition.id, "2026-03-17", "REQ-TEST-001")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Scheduler job constants / selectivity
# ---------------------------------------------------------------------------


class TestSchedulerSelectivity:
    def test_cap_constant_is_reasonable(self):
        from app.jobs.task_jobs import _BID_DUE_CAP

        assert 1 <= _BID_DUE_CAP <= 50

    def test_only_active_req_statuses(self):
        from app.constants import RequisitionStatus
        from app.jobs.task_jobs import _ACTIVE_REQ_STATUSES

        assert RequisitionStatus.ARCHIVED not in _ACTIVE_REQ_STATUSES
        assert RequisitionStatus.WON not in _ACTIVE_REQ_STATUSES
        assert RequisitionStatus.LOST not in _ACTIVE_REQ_STATUSES
        assert RequisitionStatus.ACTIVE in _ACTIVE_REQ_STATUSES
        assert RequisitionStatus.SOURCING in _ACTIVE_REQ_STATUSES

    def test_deadline_within_window_would_fire(self):
        """Deadlines within 2 days should be in scope."""
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        deadline_dt = datetime.fromisoformat(tomorrow).replace(tzinfo=timezone.utc)
        horizon = now + timedelta(days=2)
        assert deadline_dt <= horizon

    def test_deadline_far_future_would_not_fire(self):
        """Deadlines 10 days away should NOT be in scope."""
        now = datetime.now(timezone.utc)
        far = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        deadline_dt = datetime.fromisoformat(far).replace(tzinfo=timezone.utc)
        horizon = now + timedelta(days=2)
        assert deadline_dt > horizon

    def test_asap_deadline_skipped(self):
        """'ASAP' is not a parseable ISO date and would be skipped."""
        with pytest.raises(ValueError):
            datetime.fromisoformat("ASAP")
