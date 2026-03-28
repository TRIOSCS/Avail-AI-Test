"""test_task_service.py — Tests for the Requisition Task Board.

Tests CRUD operations, auto-generation, auto-close, task completion,
waiting-on queries, and API endpoints for the simplified task system.

Depends on: conftest.py fixtures, app/services/task_service.py, app/routers/task.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.services import task_service

# Helper: future due_at that passes 24h validation
FUTURE_DUE = datetime.now(timezone.utc) + timedelta(hours=48)


# ---------------------------------------------------------------------------
# Service layer tests
# ---------------------------------------------------------------------------


class TestTaskCRUD:
    def test_create_task(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Source LM317T",
            task_type="sourcing",
            priority=2,
            created_by=test_user.id,
        )
        assert task.id is not None
        assert task.title == "Source LM317T"
        assert task.task_type == "sourcing"
        assert task.status == "todo"
        assert task.source == "manual"

    def test_get_tasks(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="Task A", task_type="sourcing")
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="Task B", task_type="sales")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 2

    def test_get_tasks_filter_type(self, db_session: Session, test_requisition: Requisition):
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="A", task_type="sourcing")
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="B", task_type="sales")
        tasks = task_service.get_tasks(db_session, test_requisition.id, task_type="sourcing")
        assert len(tasks) == 1
        assert tasks[0].title == "A"

    def test_update_task(self, db_session: Session, test_requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Original")
        updated = task_service.update_task(db_session, task.id, title="Updated", priority=3)
        assert updated.title == "Updated"
        assert updated.priority == 3

    def test_update_task_status_to_done_sets_completed_at(self, db_session: Session, test_requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Test")
        assert task.completed_at is None
        updated = task_service.update_task_status(db_session, task.id, "done")
        assert updated.status == "done"
        assert updated.completed_at is not None

    def test_update_task_status_back_from_done_clears_completed(
        self, db_session: Session, test_requisition: Requisition
    ):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Test")
        task_service.update_task_status(db_session, task.id, "done")
        updated = task_service.update_task_status(db_session, task.id, "todo")
        assert updated.completed_at is None

    def test_delete_task(self, db_session: Session, test_requisition: Requisition):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Delete me")
        assert task_service.delete_task(db_session, task.id) is True
        assert task_service.get_task(db_session, task.id) is None

    def test_delete_nonexistent_returns_false(self, db_session: Session):
        assert task_service.delete_task(db_session, 99999) is False


class TestAutoGeneration:
    def test_auto_create_task(self, db_session: Session, test_requisition: Requisition):
        task = task_service.auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Review offer",
            task_type="sourcing",
            source_ref="offer:1",
        )
        assert task is not None
        assert task.source == "system"

    def test_auto_create_skips_duplicate(self, db_session: Session, test_requisition: Requisition):
        task_service.auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Review offer",
            task_type="sourcing",
            source_ref="offer:1",
        )
        dup = task_service.auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Review offer again",
            task_type="sourcing",
            source_ref="offer:1",
        )
        assert dup is None

    def test_auto_close_task(self, db_session: Session, test_requisition: Requisition):
        task_service.auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Awaiting response",
            task_type="sourcing",
            source_ref="rfq:5",
        )
        closed = task_service.auto_close_task(db_session, test_requisition.id, "rfq:5")
        assert closed is not None
        assert closed.status == "done"
        assert closed.completed_at is not None

    def test_auto_close_nonexistent_returns_none(self, db_session: Session, test_requisition: Requisition):
        result = task_service.auto_close_task(db_session, test_requisition.id, "rfq:999")
        assert result is None

    def test_on_requirement_added(self, db_session: Session, test_requisition: Requisition):
        task_service.on_requirement_added(db_session, test_requisition.id, "LM317T")
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "LM317T" in tasks[0].title
        assert tasks[0].source == "system"

    def test_on_offer_received(self, db_session: Session, test_requisition: Requisition):
        task_service.on_offer_received(db_session, test_requisition.id, "Arrow", "LM317T", 42)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        assert len(tasks) == 1
        assert "Arrow" in tasks[0].title
        assert tasks[0].source_ref == "offer:42"


class TestMyTasks:
    def test_get_my_tasks(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="My task",
            assigned_to_id=test_user.id,
        )
        tasks = task_service.get_my_tasks(db_session, test_user.id)
        assert len(tasks) == 1

    def test_get_my_tasks_excludes_done(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Done task",
            assigned_to_id=test_user.id,
        )
        task_service.update_task_status(db_session, task.id, "done")
        tasks = task_service.get_my_tasks(db_session, test_user.id)
        assert len(tasks) == 0

    def test_get_my_tasks_summary_new_shape(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Summary returns assigned_to_me, waiting_on, overdue."""
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Assigned to me",
            assigned_to_id=test_user.id,
        )
        summary = task_service.get_my_tasks_summary(db_session, test_user.id)
        assert summary["assigned_to_me"] == 1
        assert summary["waiting_on"] == 0
        assert summary["overdue"] == 0


class TestTaskResponse:
    def test_task_to_response(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Test response",
            created_by=test_user.id,
        )
        resp = task_service.task_to_response(task)
        assert resp["id"] == task.id
        assert resp["title"] == "Test response"
        assert resp["status"] == "todo"
        assert resp["creator_name"] is not None
        assert "completion_note" in resp
        assert "requisition_name" in resp


# ---------------------------------------------------------------------------
# Complete task tests
# ---------------------------------------------------------------------------


class TestCompleteTask:
    def test_complete_task_by_assignee(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Verify pricing",
            assigned_to_id=test_user.id,
            created_by=test_user.id,
        )
        completed = task_service.complete_task(db_session, task.id, test_user.id, "Pricing confirmed with vendor")
        assert completed.status == "done"
        assert completed.completed_at is not None
        assert completed.completion_note == "Pricing confirmed with vendor"

    def test_complete_task_by_non_assignee_rejected(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        # Create a second user
        other_user = User(email="other@test.com", name="Other")
        db_session.add(other_user)
        db_session.flush()

        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Only for assignee",
            assigned_to_id=test_user.id,
        )
        with pytest.raises(PermissionError, match="Only the assignee"):
            task_service.complete_task(db_session, task.id, other_user.id, "I'm not the assignee")

    def test_complete_nonexistent_returns_none(self, db_session: Session, test_user: User):
        result = task_service.complete_task(db_session, 99999, test_user.id, "note")
        assert result is None


# ---------------------------------------------------------------------------
# Waiting-on tests
# ---------------------------------------------------------------------------


class TestWaitingOn:
    def test_get_waiting_on_tasks(self, db_session: Session, test_user: User, test_requisition: Requisition):
        other_user = User(email="assignee@test.com", name="Assignee")
        db_session.add(other_user)
        db_session.flush()

        # Task created by test_user, assigned to other_user
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Waiting for this",
            created_by=test_user.id,
            assigned_to_id=other_user.id,
        )
        # Task assigned to self (should NOT appear in waiting-on)
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="My own task",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        waiting = task_service.get_waiting_on_tasks(db_session, test_user.id)
        assert len(waiting) == 1
        assert waiting[0].title == "Waiting for this"

    def test_waiting_on_excludes_done(self, db_session: Session, test_user: User, test_requisition: Requisition):
        other_user = User(email="worker@test.com", name="Worker")
        db_session.add(other_user)
        db_session.flush()

        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Done waiting",
            created_by=test_user.id,
            assigned_to_id=other_user.id,
        )
        task_service.update_task_status(db_session, task.id, "done")
        waiting = task_service.get_waiting_on_tasks(db_session, test_user.id)
        assert len(waiting) == 0


# ---------------------------------------------------------------------------
# 24h due_at validation tests
# ---------------------------------------------------------------------------


class TestDueAtValidation:
    def test_create_task_due_at_under_24h_rejected(self, db_session: Session, test_requisition: Requisition):
        too_soon = datetime.now(timezone.utc) + timedelta(hours=1)
        with pytest.raises(ValueError, match="24 hours"):
            task_service.create_task(
                db_session,
                requisition_id=test_requisition.id,
                title="Too soon",
                due_at=too_soon,
            )

    def test_create_task_due_at_over_24h_accepted(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        ok_time = datetime.now(timezone.utc) + timedelta(hours=48)
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="OK time",
            due_at=ok_time,
            assigned_to_id=test_user.id,
        )
        assert task.id is not None
        assert task.due_at is not None

    def test_system_tasks_bypass_24h_check(self, db_session: Session, test_requisition: Requisition):
        """System-generated tasks can set any due_at."""
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="System task",
            source="system",
            due_at=soon,
        )
        assert task.id is not None


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestTaskAPI:
    def test_list_tasks(self, client, test_requisition: Requisition, db_session: Session):
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="API task")
        with patch("app.routers.task.task_service.apply_simple_scoring"):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "API task"

    def test_create_task_api(self, client, test_requisition: Requisition, test_user: User):
        due = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            json={"title": "New API task", "assigned_to_id": test_user.id, "due_at": due},
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "New API task"
        assert resp.json()["assigned_to_id"] == test_user.id

    def test_create_task_api_missing_assignee(self, client, test_requisition: Requisition):
        due = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            json={"title": "No assignee", "due_at": due},
        )
        assert resp.status_code == 422  # validation error

    def test_create_task_api_missing_due(self, client, test_requisition: Requisition, test_user: User):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            json={"title": "No due", "assigned_to_id": test_user.id},
        )
        assert resp.status_code == 422

    def test_update_task_api(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Update me")
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}",
            json={"title": "Updated via API"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated via API"

    def test_delete_task_api(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Delete me")
        resp = client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}")
        assert resp.status_code == 204

    def test_my_tasks_api(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="My assigned",
            assigned_to_id=test_user.id,
        )
        resp = client.get("/api/tasks/mine")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_my_tasks_summary_api(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Summary test",
            assigned_to_id=test_user.id,
        )
        resp = client.get("/api/tasks/mine/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_to_me"] == 1
        assert data["waiting_on"] == 0

    def test_task_not_found(self, client, test_requisition: Requisition):
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/tasks/99999",
            json={"title": "Ghost"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AI scoring & simple priority scoring — lines 436-524
# ---------------------------------------------------------------------------


def _ensure_tz_aware(task):
    """SQLite stores naive datetimes — fix them for tz-aware arithmetic in tests."""
    if task.created_at and task.created_at.tzinfo is None:
        task.created_at = task.created_at.replace(tzinfo=timezone.utc)
    if task.due_at and task.due_at.tzinfo is None:
        task.due_at = task.due_at.replace(tzinfo=timezone.utc)


class TestScoreTasksWithAI:
    @pytest.mark.asyncio
    async def test_empty_task_list_returns_early(self, db_session: Session):
        """Empty task list should return immediately without calling AI."""
        result = await task_service.score_tasks_with_ai(db_session, [])
        assert result is None

    @pytest.mark.asyncio
    async def test_scores_tasks_from_ai(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """AI returns scores and risk flags that are applied to tasks."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Urgent sourcing",
            task_type="sourcing",
            assigned_to_id=test_user.id,
        )
        _ensure_tz_aware(task)
        ai_response = [{"priority_score": 0.85, "risk_flag": "Bid due tomorrow"}]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response):
            await task_service.score_tasks_with_ai(db_session, [task])
        db_session.refresh(task)
        assert task.ai_priority_score == 0.85
        assert task.ai_risk_flag == "Bid due tomorrow"

    @pytest.mark.asyncio
    async def test_ai_returns_none_no_crash(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """AI returning None should not crash."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Test",
            assigned_to_id=test_user.id,
        )
        _ensure_tz_aware(task)
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            await task_service.score_tasks_with_ai(db_session, [task])
        db_session.refresh(task)

    @pytest.mark.asyncio
    async def test_ai_returns_non_list_no_crash(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """AI returning a non-list should not crash."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Test",
            assigned_to_id=test_user.id,
        )
        _ensure_tz_aware(task)
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value={"error": "bad"}):
            await task_service.score_tasks_with_ai(db_session, [task])

    @pytest.mark.asyncio
    async def test_ai_exception_handled(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """AI exception should be caught and logged, not raised."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Test",
            assigned_to_id=test_user.id,
        )
        _ensure_tz_aware(task)
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=RuntimeError("API down")):
            await task_service.score_tasks_with_ai(db_session, [task])
        # Should not raise

    @pytest.mark.asyncio
    async def test_ai_scores_with_due_at(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Tasks with due_at get days_until_due in the prompt."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Due soon",
            task_type="sourcing",
            assigned_to_id=test_user.id,
            source="system",
            due_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        _ensure_tz_aware(task)
        ai_response = [{"priority_score": 0.7, "risk_flag": None}]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response):
            await task_service.score_tasks_with_ai(db_session, [task])
        db_session.refresh(task)
        assert task.ai_priority_score == 0.7


class TestComputeSimplePriority:
    def test_base_score(self, db_session: Session, test_requisition: Requisition):
        """Task with default priority=2 and no due_at gets base + priority boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Base",
            priority=1,
        )
        score = task_service.compute_simple_priority(task)
        assert 0.25 <= score <= 0.35  # base ~0.3

    def test_high_priority_boost(self, db_session: Session, test_requisition: Requisition):
        """Priority 3 adds 0.3 boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="High pri",
            priority=3,
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.55  # 0.3 + 0.3

    def test_medium_priority_boost(self, db_session: Session, test_requisition: Requisition):
        """Priority 2 adds 0.15 boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Med pri",
            priority=2,
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.4  # 0.3 + 0.15

    def test_overdue_boost(self, db_session: Session, test_requisition: Requisition):
        """Overdue task gets +0.4 boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Overdue",
            priority=1,
            source="system",
            due_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.65  # 0.3 + 0.4

    def test_due_today_boost(self, db_session: Session, test_requisition: Requisition):
        """Task due within 24h gets +0.3 boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Due today",
            priority=1,
            source="system",
            due_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.55  # 0.3 + 0.3

    def test_due_soon_boost(self, db_session: Session, test_requisition: Requisition):
        """Task due within 3 days gets +0.15 boost."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Due soon",
            priority=1,
            source="system",
            due_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.4  # 0.3 + 0.15

    def test_sales_type_boost(self, db_session: Session, test_requisition: Requisition):
        """Sales task type adds +0.05."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Sales task",
            task_type="sales",
            priority=1,
        )
        score = task_service.compute_simple_priority(task)
        assert score >= 0.3  # 0.3 + 0.05

    def test_max_score_capped_at_1(self, db_session: Session, test_requisition: Requisition):
        """Score never exceeds 1.0."""
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Max score",
            task_type="sales",
            priority=3,
            source="system",
            due_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        score = task_service.compute_simple_priority(task)
        assert score <= 1.0


class TestApplySimpleScoring:
    def test_scores_and_risk_flags(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Apply scoring sets ai_priority_score and risk flags on tasks."""
        overdue_task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Overdue",
            priority=3,
            source="system",
            due_at=datetime.now(timezone.utc) - timedelta(days=1),
            assigned_to_id=test_user.id,
        )
        due_today = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Due today",
            priority=2,
            source="system",
            due_at=datetime.now(timezone.utc) + timedelta(hours=12),
            assigned_to_id=test_user.id,
        )
        task_service.apply_simple_scoring(db_session, [overdue_task, due_today])
        db_session.refresh(overdue_task)
        db_session.refresh(due_today)
        assert overdue_task.ai_priority_score is not None
        assert overdue_task.ai_risk_flag == "Overdue"
        assert due_today.ai_risk_flag == "Due today"

    def test_stale_task_risk_flag(self, db_session: Session, test_user: User, test_requisition: Requisition):
        """Task open 3+ days with no activity gets risk flag."""

        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Stale",
            assigned_to_id=test_user.id,
        )
        # Manually backdate created_at
        task.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        db_session.commit()

        task_service.apply_simple_scoring(db_session, [task])
        db_session.refresh(task)
        assert task.ai_risk_flag == "No activity in 3+ days"


class TestCompleteAPI:
    def test_complete_endpoint(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Complete me",
            assigned_to_id=test_user.id,
        )
        resp = client.post(
            f"/api/tasks/{task.id}/complete",
            json={"completion_note": "Done and verified"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "done"
        assert data["completion_note"] == "Done and verified"

    def test_complete_wrong_user(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        other_user = User(email="stranger@test.com", name="Stranger")
        db_session.add(other_user)
        db_session.flush()
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Not yours",
            assigned_to_id=other_user.id,
        )
        resp = client.post(
            f"/api/tasks/{task.id}/complete",
            json={"completion_note": "I'm not the assignee"},
        )
        assert resp.status_code == 403

    def test_complete_nonexistent_task(self, client):
        resp = client.post(
            "/api/tasks/99999/complete",
            json={"completion_note": "Ghost"},
        )
        assert resp.status_code == 404


class TestWaitingAPI:
    def test_waiting_endpoint(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        other_user = User(email="delegate@test.com", name="Delegate")
        db_session.add(other_user)
        db_session.flush()
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Delegated task",
            created_by=test_user.id,
            assigned_to_id=other_user.id,
        )
        resp = client.get("/api/tasks/waiting")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Delegated task"
