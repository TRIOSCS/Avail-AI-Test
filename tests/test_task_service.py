"""Tests for app/services/task_service.py.

Covers: create_task, get_tasks, get_my_tasks, get_my_tasks_summary, get_task,
update_task, update_task_status, complete_task, get_waiting_on_tasks,
delete_task, auto_create_task, auto_close_task, on_requirement_added,
on_offer_received, on_email_offer_parsed, on_buy_plan_assigned.

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models.task import RequisitionTask
from app.services.task_service import (
    auto_close_task,
    auto_create_task,
    complete_task,
    create_task,
    delete_task,
    get_my_tasks,
    get_my_tasks_summary,
    get_task,
    get_tasks,
    get_waiting_on_tasks,
    on_buy_plan_assigned,
    on_email_offer_parsed,
    on_offer_received,
    on_requirement_added,
    update_task,
    update_task_status,
)


def _future_due(hours: int = 48) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class TestCreateTask:
    def test_creates_task(self, db_session: Session, test_requisition):
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Test task",
            source="system",
        )
        assert task.id is not None
        assert task.title == "Test task"
        assert task.status == "todo"

    def test_manual_task_requires_24h_due(self, db_session: Session, test_requisition):
        with pytest.raises(ValueError, match="24 hours"):
            create_task(
                db_session,
                requisition_id=test_requisition.id,
                title="Bad due",
                source="manual",
                due_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

    def test_manual_task_valid_due(self, db_session: Session, test_requisition, test_user):
        due = _future_due(48)
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Valid manual",
            source="manual",
            assigned_to_id=test_user.id,
            due_at=due,
        )
        assert task.id is not None

    def test_system_task_no_due_check(self, db_session: Session, test_requisition):
        # System tasks bypass the 24h check
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="System task",
            source="system",
            due_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert task.id is not None

    def test_defaults(self, db_session: Session, test_requisition):
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Defaults",
        )
        assert task.task_type == "general"
        assert task.priority == 2
        assert task.source == "manual"


class TestGetTasks:
    def test_returns_tasks_for_requisition(self, db_session: Session, test_requisition):
        create_task(db_session, requisition_id=test_requisition.id, title="T1", source="system")
        create_task(db_session, requisition_id=test_requisition.id, title="T2", source="system")
        tasks = get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 2

    def test_filters_by_status(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        update_task_status(db_session, t.id, TaskStatus.DONE)
        active = get_tasks(db_session, test_requisition.id, status="todo")
        done = get_tasks(db_session, test_requisition.id, status=TaskStatus.DONE)
        assert len(active) == 0
        assert len(done) == 1

    def test_filters_by_task_type(self, db_session: Session, test_requisition):
        create_task(db_session, requisition_id=test_requisition.id, title="T", task_type="sourcing", source="system")
        sourcing = get_tasks(db_session, test_requisition.id, task_type="sourcing")
        general = get_tasks(db_session, test_requisition.id, task_type="general")
        assert len(sourcing) == 1
        assert len(general) == 0

    def test_filters_by_assigned_to(self, db_session: Session, test_requisition, test_user):
        create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Assigned",
            source="system",
            assigned_to_id=test_user.id,
        )
        create_task(db_session, requisition_id=test_requisition.id, title="Unassigned", source="system")
        tasks = get_tasks(db_session, test_requisition.id, assigned_to_id=test_user.id)
        assert len(tasks) == 1


class TestGetMyTasks:
    def test_returns_non_done_tasks(self, db_session: Session, test_requisition, test_user):
        t1 = create_task(
            db_session, requisition_id=test_requisition.id, title="T1", source="system", assigned_to_id=test_user.id
        )
        t2 = create_task(
            db_session, requisition_id=test_requisition.id, title="T2", source="system", assigned_to_id=test_user.id
        )
        update_task_status(db_session, t2.id, TaskStatus.DONE)
        tasks = get_my_tasks(db_session, test_user.id)
        ids = [t.id for t in tasks]
        assert t1.id in ids
        assert t2.id not in ids

    def test_filter_by_status(self, db_session: Session, test_requisition, test_user):
        t = create_task(
            db_session, requisition_id=test_requisition.id, title="T", source="system", assigned_to_id=test_user.id
        )
        update_task_status(db_session, t.id, TaskStatus.DONE)
        done_tasks = get_my_tasks(db_session, test_user.id, status=TaskStatus.DONE)
        assert any(t2.id == t.id for t2 in done_tasks)

    def test_empty_when_no_tasks(self, db_session: Session, test_user):
        tasks = get_my_tasks(db_session, test_user.id)
        assert tasks == []


class TestGetMyTasksSummary:
    def test_returns_counts(self, db_session: Session, test_requisition, test_user, sales_user):
        create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Mine",
            source="system",
            assigned_to_id=test_user.id,
            created_by=test_user.id,
        )
        create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="WaitingOn",
            source="system",
            assigned_to_id=sales_user.id,
            created_by=test_user.id,
        )
        summary = get_my_tasks_summary(db_session, test_user.id)
        assert summary["assigned_to_me"] == 1
        assert summary["waiting_on"] == 1
        assert summary["overdue"] == 0

    def test_overdue_count(self, db_session: Session, test_requisition, test_user):
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Overdue",
            source="system",
            assigned_to_id=test_user.id,
            due_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db_session.add(task)
        db_session.commit()
        summary = get_my_tasks_summary(db_session, test_user.id)
        assert summary["overdue"] == 1

    def test_empty_for_new_user(self, db_session: Session, test_user):
        summary = get_my_tasks_summary(db_session, test_user.id)
        assert summary == {"assigned_to_me": 0, "waiting_on": 0, "overdue": 0}


class TestGetTask:
    def test_returns_task(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        found = get_task(db_session, t.id)
        assert found is not None
        assert found.id == t.id

    def test_returns_none_for_missing(self, db_session: Session):
        assert get_task(db_session, 99999) is None


class TestUpdateTask:
    def test_updates_fields(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="Old", source="system")
        updated = update_task(db_session, t.id, title="New", priority=3)
        assert updated.title == "New"
        assert updated.priority == 3

    def test_returns_none_for_missing(self, db_session: Session):
        assert update_task(db_session, 99999, title="X") is None

    def test_sets_completed_at_when_done(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        updated = update_task(db_session, t.id, status=TaskStatus.DONE)
        assert updated.completed_at is not None

    def test_clears_completed_at_when_moved_back(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        update_task(db_session, t.id, status=TaskStatus.DONE)
        updated = update_task(db_session, t.id, status="todo")
        assert updated.completed_at is None


class TestUpdateTaskStatus:
    def test_status_change(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        updated = update_task_status(db_session, t.id, "in_progress")
        assert updated.status == "in_progress"


class TestCompleteTask:
    def test_completes_task(self, db_session: Session, test_requisition, test_user):
        t = create_task(
            db_session, requisition_id=test_requisition.id, title="T", source="system", assigned_to_id=test_user.id
        )
        result = complete_task(db_session, t.id, test_user.id, "All done")
        assert result.status == TaskStatus.DONE
        assert result.completion_note == "All done"
        assert result.completed_at is not None

    def test_raises_if_not_assignee(self, db_session: Session, test_requisition, test_user, sales_user):
        t = create_task(
            db_session, requisition_id=test_requisition.id, title="T", source="system", assigned_to_id=test_user.id
        )
        with pytest.raises(PermissionError):
            complete_task(db_session, t.id, sales_user.id, "Not my task")

    def test_returns_none_for_missing(self, db_session: Session, test_user):
        assert complete_task(db_session, 99999, test_user.id, "note") is None


class TestGetWaitingOnTasks:
    def test_returns_waiting_on(self, db_session: Session, test_requisition, test_user, sales_user):
        create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Waiting",
            source="system",
            assigned_to_id=sales_user.id,
            created_by=test_user.id,
        )
        tasks = get_waiting_on_tasks(db_session, test_user.id)
        assert len(tasks) == 1

    def test_excludes_done(self, db_session: Session, test_requisition, test_user, sales_user):
        t = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Done",
            source="system",
            assigned_to_id=sales_user.id,
            created_by=test_user.id,
        )
        update_task_status(db_session, t.id, TaskStatus.DONE)
        tasks = get_waiting_on_tasks(db_session, test_user.id)
        assert len(tasks) == 0


class TestDeleteTask:
    def test_deletes_task(self, db_session: Session, test_requisition):
        t = create_task(db_session, requisition_id=test_requisition.id, title="T", source="system")
        assert delete_task(db_session, t.id) is True
        assert get_task(db_session, t.id) is None

    def test_returns_false_for_missing(self, db_session: Session):
        assert delete_task(db_session, 99999) is False


class TestAutoCreateTask:
    def test_creates_system_task(self, db_session: Session, test_requisition):
        task = auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto task",
            task_type="sourcing",
            source_ref="offer:1",
        )
        assert task is not None
        assert task.source == "system"

    def test_skips_duplicate_source_ref(self, db_session: Session, test_requisition):
        auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:42",
        )
        result = auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:42",
        )
        assert result is None

    def test_allows_new_after_done(self, db_session: Session, test_requisition):
        t = auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:99",
        )
        update_task_status(db_session, t.id, TaskStatus.DONE)
        new_task = auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto again",
            task_type="sourcing",
            source_ref="offer:99",
        )
        assert new_task is not None


class TestAutoCloseTask:
    def test_closes_task(self, db_session: Session, test_requisition):
        auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Task to close",
            task_type="sourcing",
            source_ref="rfq:10",
        )
        closed = auto_close_task(db_session, test_requisition.id, "rfq:10")
        assert closed is not None
        assert closed.status == TaskStatus.DONE

    def test_returns_none_when_no_match(self, db_session: Session, test_requisition):
        result = auto_close_task(db_session, test_requisition.id, "nonexistent:99")
        assert result is None


class TestConvenienceHelpers:
    def test_on_requirement_added(self, db_session: Session, test_requisition):
        on_requirement_added(db_session, test_requisition.id, "STM32F407")
        tasks = get_tasks(db_session, test_requisition.id)
        assert any("STM32F407" in t.title for t in tasks)

    def test_on_offer_received(self, db_session: Session, test_requisition):
        on_offer_received(db_session, test_requisition.id, "Vendor X", "BC547", offer_id=5)
        tasks = get_tasks(db_session, test_requisition.id)
        assert any("Vendor X" in t.title for t in tasks)

    def test_on_email_offer_parsed(self, db_session: Session, test_requisition):
        on_email_offer_parsed(db_session, test_requisition.id, "Email Vendor", "LM358", offer_id=7)
        tasks = get_tasks(db_session, test_requisition.id)
        assert any("Email Vendor" in t.title for t in tasks)

    def test_on_buy_plan_assigned(self, db_session: Session, test_requisition, test_user):
        on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "Acme", "NE555", line_id=3)
        tasks = get_tasks(db_session, test_requisition.id)
        assert any("NE555" in t.title for t in tasks)
