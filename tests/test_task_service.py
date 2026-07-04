"""Tests for app/services/task_service.py.

Covers: create_task, get_my_tasks (incl. the bounded "done" window), get_my_tasks_summary,
get_task, update_task, complete_task, delete_task, auto_create_task, auto_close_task,
on_requirement_added, on_offer_received, on_email_offer_parsed, on_buy_plan_assigned.

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models.sourcing import Requisition
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
    on_buy_plan_assigned,
    on_email_offer_parsed,
    on_offer_received,
    on_requirement_added,
    update_task,
)


def _future_due(hours: int = 48) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


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


class TestGetMyTasks:
    def test_returns_non_done_tasks(self, db_session: Session, test_requisition, test_user):
        t1 = create_task(
            db_session, requisition_id=test_requisition.id, title="T1", source="system", assigned_to_id=test_user.id
        )
        t2 = create_task(
            db_session, requisition_id=test_requisition.id, title="T2", source="system", assigned_to_id=test_user.id
        )
        update_task(db_session, t2.id, status=TaskStatus.DONE)
        tasks = get_my_tasks(db_session, test_user.id)
        ids = [t.id for t in tasks]
        assert t1.id in ids
        assert t2.id not in ids

    def test_filter_by_status(self, db_session: Session, test_requisition, test_user):
        t = create_task(
            db_session, requisition_id=test_requisition.id, title="T", source="system", assigned_to_id=test_user.id
        )
        update_task(db_session, t.id, status=TaskStatus.DONE)
        done_tasks = get_my_tasks(db_session, test_user.id, status=TaskStatus.DONE)
        assert any(t2.id == t.id for t2 in done_tasks)

    def test_empty_when_no_tasks(self, db_session: Session, test_user):
        tasks = get_my_tasks(db_session, test_user.id)
        assert tasks == []

    def test_eager_loads_parent_relationships(self, db_session: Session, test_requisition, test_user):
        """A requisition-scoped task assigned to the user surfaces with its parent
        loaded (joinedload options must not error and must resolve the relationship)."""
        create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Linked",
            source="system",
            assigned_to_id=test_user.id,
        )
        tasks = get_my_tasks(db_session, test_user.id)
        assert len(tasks) == 1
        assert tasks[0].requisition is not None
        assert tasks[0].requisition.id == test_requisition.id


class TestGetMyTasksDoneBounded:
    """M5: the My Day "Done" filter is bounded to a recent window, not the user's entire
    completion history."""

    def _done_task(self, db: Session, req_id: int, user_id: int, title: str, completed_days_ago: int):
        task = create_task(db, requisition_id=req_id, title=title, source="system", assigned_to_id=user_id)
        task.status = TaskStatus.DONE
        task.completed_at = datetime.now(timezone.utc) - timedelta(days=completed_days_ago)
        db.commit()
        db.refresh(task)
        return task

    def test_recent_done_included(self, db_session: Session, test_requisition, test_user):
        recent = self._done_task(db_session, test_requisition.id, test_user.id, "Recent", completed_days_ago=5)
        tasks = get_my_tasks(db_session, test_user.id, status=TaskStatus.DONE)
        assert recent.id in [t.id for t in tasks]

    def test_old_done_excluded(self, db_session: Session, test_requisition, test_user):
        old = self._done_task(db_session, test_requisition.id, test_user.id, "Ancient", completed_days_ago=45)
        tasks = get_my_tasks(db_session, test_user.id, status=TaskStatus.DONE)
        assert old.id not in [t.id for t in tasks]


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
        update_task(db_session, t.id, status=TaskStatus.DONE)
        new_task = auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Auto again",
            task_type="sourcing",
            source_ref="offer:99",
        )
        assert new_task is not None


class TestAutoTaskDefaultAssignee:
    """Auto-created tasks default their assignee to coalesce(claimed_by_id, created_by)
    of the linked requisition, so they surface on someone's My Day (which filters
    ``assigned_to_id == user.id``) instead of being created unassigned/invisible (spec
    L1).

    The requisition's buyer-claim field is ``claimed_by_id`` ("which buyer picked up this
    requisition for sourcing"); the requisition has no ``assigned_buyer_id`` column.
    """

    def _make_req(self, db: Session, *, created_by: int, claimed_by_id: int | None = None) -> Requisition:
        req = Requisition(
            name="REQ-AUTO-ASSIGN",
            status="open",
            created_by=created_by,
            claimed_by_id=claimed_by_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.commit()
        db.refresh(req)
        return req

    def test_defaults_to_claiming_buyer(self, db_session: Session, test_user, sales_user):
        # Requisition claimed by a buyer (sales_user) and created by someone else (test_user).
        req = self._make_req(db_session, created_by=test_user.id, claimed_by_id=sales_user.id)
        task = auto_create_task(
            db_session,
            requisition_id=req.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:buyer",
        )
        assert task is not None
        assert task.assigned_to_id == sales_user.id

    def test_defaults_to_creator_when_no_buyer(self, db_session: Session, test_user):
        # No claiming buyer → falls back to the requisition creator.
        req = self._make_req(db_session, created_by=test_user.id)
        task = auto_create_task(
            db_session,
            requisition_id=req.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:creator",
        )
        assert task is not None
        assert task.assigned_to_id == test_user.id

    def test_explicit_assignee_is_respected(self, db_session: Session, test_user, sales_user):
        # An explicitly-passed assignee is never overridden by the coalesce default.
        req = self._make_req(db_session, created_by=test_user.id, claimed_by_id=test_user.id)
        task = auto_create_task(
            db_session,
            requisition_id=req.id,
            title="Auto",
            task_type="sourcing",
            source_ref="offer:explicit",
            assigned_to_id=sales_user.id,
        )
        assert task is not None
        assert task.assigned_to_id == sales_user.id


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
        tasks = _req_tasks(db_session, test_requisition.id)
        assert any("STM32F407" in t.title for t in tasks)

    def test_on_offer_received(self, db_session: Session, test_requisition):
        on_offer_received(db_session, test_requisition.id, "Vendor X", "BC547", offer_id=5)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert any("Vendor X" in t.title for t in tasks)

    def test_on_email_offer_parsed(self, db_session: Session, test_requisition):
        on_email_offer_parsed(db_session, test_requisition.id, "Email Vendor", "LM358", offer_id=7)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert any("Email Vendor" in t.title for t in tasks)

    def test_on_buy_plan_assigned(self, db_session: Session, test_requisition, test_user):
        on_buy_plan_assigned(db_session, test_requisition.id, test_user.id, "Acme", "NE555", line_id=3)
        tasks = _req_tasks(db_session, test_requisition.id)
        assert any("NE555" in t.title for t in tasks)
