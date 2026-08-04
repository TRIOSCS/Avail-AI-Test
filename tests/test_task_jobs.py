"""Tests for selective auto-task triggers — buy plan, email offer, new offers.

Verifies that inline task events create the right tasks and respect dedup.
(The bid_due_alerts scheduler job and its ``on_bid_due_soon`` hook were deleted
in the W1 simplification, 2026-08-04, per docs/W1_JOB_DISPOSITION.md — their
tests went with them.)

Depends on: conftest.py fixtures, app/services/task_service.py
"""

from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.models.task import RequisitionTask
from app.services import task_service


def _req_tasks(db, requisition_id: int) -> list[RequisitionTask]:
    """Fetch all tasks for a requisition (test helper; the old get_tasks was dead
    code)."""
    return (
        db.query(RequisitionTask)
        .filter(RequisitionTask.requisition_id == requisition_id)
        .order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at)
        .all()
    )


# ---------------------------------------------------------------------------
# New requirement tasks (already existed, verify still works)
# ---------------------------------------------------------------------------


class TestOnRequirementAdded:
    def test_creates_sourcing_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "LM317T" in tasks[0].title
        assert tasks[0].source_ref == "source:LM317T"
        assert tasks[0].source == "system"

    def test_dedup_same_mpn(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# New offer tasks
# ---------------------------------------------------------------------------


class TestOnOfferReceived:
    def test_creates_review_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Arrow" in tasks[0].title
        assert tasks[0].source_ref == "offer:42"

    def test_dedup_same_offer(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Email-parsed offer tasks
# ---------------------------------------------------------------------------


class TestOnEmailOfferParsed:
    def test_creates_email_offer_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Email offer" in tasks[0].title
        assert "Mouser" in tasks[0].title
        assert tasks[0].source_ref == "email_offer:99"

    def test_dedup_same_email_offer(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        task_service.on_email_offer_parsed(db_session, test_requisition.id, "Mouser", "STM32F4", 99)
        tasks = _req_tasks(db_session, test_requisition.id)
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
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Cut PO" in tasks[0].title
        assert "DigiKey" in tasks[0].title
        assert tasks[0].source_ref == "buyline:7"
        assert tasks[0].assigned_to_id == test_user.id
        assert tasks[0].task_type == "buying"

    def test_dedup_same_line(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "DigiKey", "LM317T", 7)
        task_service.on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "DigiKey", "LM317T", 7)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
