"""Tests for app/services/task_service.py — comprehensive coverage.

Covers CRUD, auto-generation, complete/delete, summary, and convenience event helpers.

Called by: pytest
Depends on: conftest fixtures, app.services.task_service, app.models.task
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import Requisition, User
from app.models.task import RequisitionTask
from app.services import task_service


@pytest.fixture()
def requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="TASK-TEST-REQ",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


def _req_tasks(db: Session, requisition_id: int) -> list[RequisitionTask]:
    """Fetch all tasks for a requisition (test helper; the old get_tasks was dead
    code)."""
    return (
        db.query(RequisitionTask)
        .filter(RequisitionTask.requisition_id == requisition_id)
        .order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at)
        .all()
    )


class TestCreateTask:
    def test_create_manual_task_far_future(self, db_session: Session, requisition: Requisition, test_user: User):
        due = datetime.now(timezone.utc) + timedelta(days=2)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Manual Task",
            source="manual",
            assigned_to_id=test_user.id,
            due_at=due,
        )
        assert task.id is not None
        assert task.title == "Manual Task"
        assert task.source == "manual"

    def test_create_system_task_no_due_constraint(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="System Task",
            source="system",
        )
        assert task.id is not None
        assert task.source == "system"

    def test_manual_task_within_24h_raises(self, db_session: Session, requisition: Requisition):
        due = datetime.now(timezone.utc) + timedelta(hours=10)
        with pytest.raises(ValueError, match="24 hours"):
            task_service.create_task(
                db_session,
                requisition_id=requisition.id,
                title="Too Soon",
                source="manual",
                due_at=due,
            )

    def test_manual_task_naive_due_raises(self, db_session: Session, requisition: Requisition):
        due = datetime.utcnow() + timedelta(hours=5)  # naive, < 24h
        with pytest.raises(ValueError):
            task_service.create_task(
                db_session,
                requisition_id=requisition.id,
                title="Naive Due",
                source="manual",
                due_at=due,
            )

    def test_create_with_description_and_priority(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="High Priority",
            description="Important task",
            priority=3,
            source="system",
        )
        assert task.priority == 3
        assert task.description == "Important task"


class TestGetMyTasks:
    def test_get_my_tasks_excludes_done(self, db_session: Session, requisition: Requisition, test_user: User):
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Open",
            source="system",
            assigned_to_id=test_user.id,
        )
        t2 = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Done",
            source="system",
            assigned_to_id=test_user.id,
        )
        task_service.update_task(db_session, t2.id, status=TaskStatus.DONE)
        tasks = task_service.get_my_tasks(db_session, test_user.id)
        assert len(tasks) == 1
        assert tasks[0].title == "Open"

    def test_get_my_tasks_with_status_filter(self, db_session: Session, requisition: Requisition, test_user: User):
        t = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Done Task",
            source="system",
            assigned_to_id=test_user.id,
        )
        task_service.update_task(db_session, t.id, status=TaskStatus.DONE)
        tasks = task_service.get_my_tasks(db_session, test_user.id, status=TaskStatus.DONE)
        assert len(tasks) == 1


class TestGetMyTasksSummary:
    def test_summary_empty(self, db_session: Session, test_user: User):
        summary = task_service.get_my_tasks_summary(db_session, test_user.id)
        assert summary["assigned_to_me"] == 0
        assert summary["waiting_on"] == 0
        assert summary["overdue"] == 0

    def test_summary_with_tasks(
        self,
        db_session: Session,
        requisition: Requisition,
        test_user: User,
        admin_user: User,
    ):
        # Task assigned to me
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Mine",
            source="system",
            assigned_to_id=test_user.id,
            created_by=test_user.id,
        )
        # Task created by me, assigned to someone else
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="WaitOn",
            source="system",
            assigned_to_id=admin_user.id,
            created_by=test_user.id,
        )
        summary = task_service.get_my_tasks_summary(db_session, test_user.id)
        assert summary["assigned_to_me"] == 1
        assert summary["waiting_on"] == 1


class TestUpdateTask:
    def test_update_title(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="Old", source="system")
        updated = task_service.update_task(db_session, task.id, title="New")
        assert updated.title == "New"

    def test_update_nonexistent_returns_none(self, db_session: Session):
        result = task_service.update_task(db_session, 99999, title="X")
        assert result is None

    def test_update_to_done_sets_completed_at(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        updated = task_service.update_task(db_session, task.id, status=TaskStatus.DONE)
        assert updated.completed_at is not None

    def test_update_from_done_clears_completed_at(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        task_service.update_task(db_session, task.id, status=TaskStatus.DONE)
        updated = task_service.update_task(db_session, task.id, status=TaskStatus.TODO)
        assert updated.completed_at is None


class TestCompleteTask:
    def test_complete_by_assignee(self, db_session: Session, requisition: Requisition, test_user: User):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="T",
            source="system",
            assigned_to_id=test_user.id,
        )
        completed = task_service.complete_task(db_session, task.id, test_user.id, "Done!")
        assert completed.status == TaskStatus.DONE
        assert completed.completion_note == "Done!"

    def test_complete_by_non_assignee_raises(
        self, db_session: Session, requisition: Requisition, test_user: User, admin_user: User
    ):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="T",
            source="system",
            assigned_to_id=test_user.id,
        )
        with pytest.raises(PermissionError):
            task_service.complete_task(db_session, task.id, admin_user.id, "Unauthorized!")

    def test_complete_nonexistent_returns_none(self, db_session: Session, test_user: User):
        result = task_service.complete_task(db_session, 99999, test_user.id, "X")
        assert result is None


class TestDeleteTask:
    def test_delete_existing(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        result = task_service.delete_task(db_session, task.id)
        assert result is True

    def test_delete_nonexistent_returns_false(self, db_session: Session):
        result = task_service.delete_task(db_session, 99999)
        assert result is False


class TestAutoCreateTask:
    def test_creates_on_first_call(self, db_session: Session, requisition: Requisition):
        task = task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="test:1",
        )
        assert task is not None
        assert task.source == "system"

    def test_skips_duplicate_source_ref(self, db_session: Session, requisition: Requisition):
        task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="test:1",
        )
        result = task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto Duplicate",
            task_type="sourcing",
            source_ref="test:1",
        )
        assert result is None

    def test_allows_second_after_done(self, db_session: Session, requisition: Requisition):
        t = task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="test:2",
        )
        task_service.update_task(db_session, t.id, status=TaskStatus.DONE)
        t2 = task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto Again",
            task_type="sourcing",
            source_ref="test:2",
        )
        assert t2 is not None


class TestConvenienceHelpers:
    def test_on_email_offer_parsed(self, db_session: Session, requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, requisition.id, "Arrow", "LM317T", 99)
        tasks = _req_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert "email_offer:99" == tasks[0].source_ref

    def test_on_buy_plan_assigned(self, db_session: Session, requisition: Requisition, test_user: User):
        task_service.on_buy_plan_assigned(db_session, requisition.id, test_user.id, "Arrow", "LM317T", 55)
        tasks = _req_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert tasks[0].task_type == "buying"

    def test_on_bid_due_soon(self, db_session: Session, requisition: Requisition):
        task_service.on_bid_due_soon(db_session, requisition.id, "2026-12-31", "Test REQ")
        tasks = _req_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert "Bid due" in tasks[0].title
