"""Tests for app/services/task_service.py — comprehensive coverage.

Covers CRUD, auto-generation, complete/delete, summary, AI scoring helpers,
and convenience event helpers.

Called by: pytest
Depends on: conftest fixtures, app.services.task_service, app.models.task
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


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


class TestGetTasks:
    def test_get_all_tasks(self, db_session: Session, requisition: Requisition):
        task_service.create_task(db_session, requisition_id=requisition.id, title="T1", source="system")
        task_service.create_task(db_session, requisition_id=requisition.id, title="T2", source="system")
        tasks = task_service.get_tasks(db_session, requisition.id)
        assert len(tasks) == 2

    def test_filter_by_status(self, db_session: Session, requisition: Requisition):
        t1 = task_service.create_task(db_session, requisition_id=requisition.id, title="T1", source="system")
        task_service.update_task_status(db_session, t1.id, TaskStatus.DONE)
        task_service.create_task(db_session, requisition_id=requisition.id, title="T2", source="system")
        open_tasks = task_service.get_tasks(db_session, requisition.id, status=TaskStatus.TODO)
        assert len(open_tasks) == 1
        assert open_tasks[0].title == "T2"

    def test_filter_by_task_type(self, db_session: Session, requisition: Requisition):
        task_service.create_task(
            db_session, requisition_id=requisition.id, title="Sourcing", task_type="sourcing", source="system"
        )
        task_service.create_task(
            db_session, requisition_id=requisition.id, title="Sales", task_type="sales", source="system"
        )
        sourcing = task_service.get_tasks(db_session, requisition.id, task_type="sourcing")
        assert len(sourcing) == 1
        assert sourcing[0].task_type == "sourcing"

    def test_filter_by_assigned_to_id(self, db_session: Session, requisition: Requisition, test_user: User):
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Assigned",
            source="system",
            assigned_to_id=test_user.id,
        )
        task_service.create_task(db_session, requisition_id=requisition.id, title="Unassigned", source="system")
        assigned = task_service.get_tasks(db_session, requisition.id, assigned_to_id=test_user.id)
        assert len(assigned) == 1


class TestGetMyTasks:
    def test_get_my_tasks_excludes_done(self, db_session: Session, requisition: Requisition, test_user: User):
        t1 = task_service.create_task(
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
        task_service.update_task_status(db_session, t2.id, TaskStatus.DONE)
        tasks = task_service.get_my_tasks(db_session, test_user.id)
        assert len(tasks) == 1
        assert tasks[0].title == "Open"

    def test_get_my_tasks_with_status_filter(self, db_session: Session, requisition: Requisition, test_user: User):
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Done Task",
            source="system",
            assigned_to_id=test_user.id,
        )
        t = task_service.get_tasks(db_session, requisition.id)[0]
        task_service.update_task_status(db_session, t.id, TaskStatus.DONE)
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
    def test_complete_by_assignee(
        self, db_session: Session, requisition: Requisition, test_user: User
    ):
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


class TestGetWaitingOnTasks:
    def test_waiting_on(
        self,
        db_session: Session,
        requisition: Requisition,
        test_user: User,
        admin_user: User,
    ):
        task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Waiting",
            source="system",
            assigned_to_id=admin_user.id,
            created_by=test_user.id,
        )
        tasks = task_service.get_waiting_on_tasks(db_session, test_user.id)
        assert len(tasks) == 1

    def test_waiting_on_excludes_done(
        self,
        db_session: Session,
        requisition: Requisition,
        test_user: User,
        admin_user: User,
    ):
        t = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Waiting Done",
            source="system",
            assigned_to_id=admin_user.id,
            created_by=test_user.id,
        )
        task_service.update_task_status(db_session, t.id, TaskStatus.DONE)
        tasks = task_service.get_waiting_on_tasks(db_session, test_user.id)
        assert len(tasks) == 0


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
        task_service.update_task_status(db_session, t.id, TaskStatus.DONE)
        t2 = task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto Again",
            task_type="sourcing",
            source_ref="test:2",
        )
        assert t2 is not None


class TestAutoCloseTask:
    def test_closes_open_task(self, db_session: Session, requisition: Requisition):
        task_service.auto_create_task(
            db_session,
            requisition_id=requisition.id,
            title="Auto",
            task_type="sourcing",
            source_ref="close:1",
        )
        closed = task_service.auto_close_task(db_session, requisition.id, "close:1")
        assert closed is not None
        assert closed.status == TaskStatus.DONE

    def test_auto_close_no_match_returns_none(self, db_session: Session, requisition: Requisition):
        result = task_service.auto_close_task(db_session, requisition.id, "nonexistent:99")
        assert result is None


class TestConvenienceHelpers:
    def test_on_email_offer_parsed(self, db_session: Session, requisition: Requisition):
        task_service.on_email_offer_parsed(db_session, requisition.id, "Arrow", "LM317T", 99)
        tasks = task_service.get_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert "email_offer:99" == tasks[0].source_ref

    def test_on_buy_plan_assigned(
        self, db_session: Session, requisition: Requisition, test_user: User
    ):
        task_service.on_buy_plan_assigned(db_session, requisition.id, test_user.id, "Arrow", "LM317T", 55)
        tasks = task_service.get_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert tasks[0].task_type == "buying"

    def test_on_bid_due_soon(self, db_session: Session, requisition: Requisition):
        task_service.on_bid_due_soon(db_session, requisition.id, "2026-12-31", "Test REQ")
        tasks = task_service.get_tasks(db_session, requisition.id)
        assert len(tasks) == 1
        assert "Bid due" in tasks[0].title


class TestTaskToResponse:
    def test_task_to_response_minimal(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        resp = task_service.task_to_response(task)
        assert resp["id"] == task.id
        assert resp["title"] == "T"
        assert resp["assignee_name"] is None
        assert resp["creator_name"] is None

    def test_task_to_response_with_assignee(
        self, db_session: Session, requisition: Requisition, test_user: User
    ):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="T",
            source="system",
            assigned_to_id=test_user.id,
            created_by=test_user.id,
        )
        db_session.refresh(task)
        resp = task_service.task_to_response(task)
        assert resp["assigned_to_id"] == test_user.id

    def test_task_to_response_with_due_and_completed(
        self, db_session: Session, requisition: Requisition, test_user: User
    ):
        due = datetime.now(timezone.utc) + timedelta(days=3)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="T",
            source="system",
            due_at=due,
            assigned_to_id=test_user.id,
        )
        task_service.complete_task(db_session, task.id, test_user.id, "Done")
        db_session.refresh(task)
        resp = task_service.task_to_response(task)
        assert resp["due_at"] is not None
        assert resp["completed_at"] is not None


class TestComputeSimplePriority:
    def test_high_priority_task(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="High",
            priority=3,
            source="system",
        )
        score = task_service.compute_simple_priority(task)
        assert score > 0.5

    def test_overdue_task_scores_high(self, db_session: Session, requisition: Requisition):
        past_due = datetime.now(timezone.utc) - timedelta(days=2)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Overdue",
            source="system",
        )
        task.due_at = past_due
        db_session.commit()
        score = task_service.compute_simple_priority(task)
        assert score >= 0.7

    def test_sales_task_type_boost(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Sales",
            task_type="sales",
            source="system",
        )
        score = task_service.compute_simple_priority(task)
        assert score > 0.3

    def test_score_capped_at_one(self, db_session: Session, requisition: Requisition):
        past_due = datetime.now(timezone.utc) - timedelta(days=5)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Very Overdue",
            priority=3,
            task_type="sales",
            source="system",
        )
        task.due_at = past_due
        db_session.commit()
        score = task_service.compute_simple_priority(task)
        assert score == 1.0

    def test_due_today_score(self, db_session: Session, requisition: Requisition):
        soon = datetime.now(timezone.utc) + timedelta(hours=6)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Due Soon",
            source="system",
        )
        task.due_at = soon
        db_session.commit()
        score = task_service.compute_simple_priority(task)
        assert score >= 0.6

    def test_due_within_3_days(self, db_session: Session, requisition: Requisition):
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        task = task_service.create_task(
            db_session,
            requisition_id=requisition.id,
            title="Due 2 Days",
            source="system",
        )
        task.due_at = soon
        db_session.commit()
        score = task_service.compute_simple_priority(task)
        assert score > 0.3


class TestApplySimpleScoring:
    def test_applies_to_all_tasks(self, db_session: Session, requisition: Requisition):
        t1 = task_service.create_task(db_session, requisition_id=requisition.id, title="T1", source="system")
        t2 = task_service.create_task(db_session, requisition_id=requisition.id, title="T2", priority=3, source="system")
        task_service.apply_simple_scoring(db_session, [t1, t2])
        assert t1.ai_priority_score is not None
        assert t2.ai_priority_score is not None

    def test_overdue_sets_risk_flag(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        task.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.commit()
        task_service.apply_simple_scoring(db_session, [task])
        assert task.ai_risk_flag == "Overdue"

    def test_due_today_risk_flag(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        task.due_at = datetime.now(timezone.utc) + timedelta(hours=6)
        db_session.commit()
        task_service.apply_simple_scoring(db_session, [task])
        assert task.ai_risk_flag == "Due today"

    def test_stale_task_risk_flag(self, db_session: Session, requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=requisition.id, title="T", source="system")
        task.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        task.status = TaskStatus.TODO
        db_session.commit()
        task_service.apply_simple_scoring(db_session, [task])
        assert task.ai_risk_flag == "No activity in 3+ days"


def _make_mock_task(requisition_id: int, user_id: int | None = None) -> MagicMock:
    """Build a mock RequisitionTask with aware datetimes (bypasses SQLite naive issue)."""
    t = MagicMock(spec=RequisitionTask)
    t.id = 9999
    t.requisition_id = requisition_id
    t.title = "Mock Task"
    t.description = None
    t.task_type = "sourcing"
    t.status = TaskStatus.TODO
    t.priority = 2
    t.ai_priority_score = None
    t.ai_risk_flag = None
    t.assigned_to_id = user_id
    t.created_by = user_id
    t.source = "system"
    t.source_ref = None
    t.completion_note = None
    t.due_at = None
    t.completed_at = None
    t.created_at = datetime.now(timezone.utc)
    t.updated_at = datetime.now(timezone.utc)
    t.assignee = None
    t.creator = None
    t.requisition = None
    return t


class TestScoreTasksWithAI:
    async def test_score_tasks_empty(self, db_session: Session):
        # Should return immediately without error
        await task_service.score_tasks_with_ai(db_session, [])

    async def test_score_tasks_with_ai_success(self, db_session: Session, requisition: Requisition):
        mock_task = _make_mock_task(requisition.id)
        mock_result = [{"priority_score": 0.8, "risk_flag": "Test risk"}]

        async def _mock_claude_json(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_json", new=_mock_claude_json):
            await task_service.score_tasks_with_ai(db_session, [mock_task])

    async def test_score_tasks_handles_exception(self, db_session: Session, requisition: Requisition):
        mock_task = _make_mock_task(requisition.id)

        async def _fail(*a, **kw):
            raise RuntimeError("API down")

        with patch("app.utils.claude_client.claude_json", new=_fail):
            # Should not raise — exception is caught internally
            await task_service.score_tasks_with_ai(db_session, [mock_task])

    async def test_score_tasks_non_list_response(self, db_session: Session, requisition: Requisition):
        mock_task = _make_mock_task(requisition.id)

        async def _bad(*a, **kw):
            return {"error": "unexpected"}

        with patch("app.utils.claude_client.claude_json", new=_bad):
            await task_service.score_tasks_with_ai(db_session, [mock_task])

    async def test_score_tasks_with_due_date(self, db_session: Session, requisition: Requisition):
        mock_task = _make_mock_task(requisition.id)
        mock_task.due_at = datetime.now(timezone.utc) + timedelta(days=2)
        mock_result = [{"priority_score": 0.9, "risk_flag": None}]

        async def _mock_claude_json(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_json", new=_mock_claude_json):
            await task_service.score_tasks_with_ai(db_session, [mock_task])
