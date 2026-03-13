"""
test_task_service.py — Tests for the Requisition Task Board

Tests CRUD operations, auto-generation, auto-close, task completion,
waiting-on queries, and API endpoints for the simplified task system.

Depends on: conftest.py fixtures, app/services/task_service.py, app/routers/task.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


class TestPatchStatusAPI:
    """Tests for the PATCH status endpoints used by the Task Queue UI."""

    def test_patch_req_task_status(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Patch me")
        resp = client.patch(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/status",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

    def test_patch_req_task_status_to_done(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Done via patch")
        resp = client.patch(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/status",
            json={"status": "done"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
        assert resp.json()["completed_at"] is not None

    def test_patch_req_task_invalid_status(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Bad status")
        resp = client.patch(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/status",
            json={"status": "invalid"},
        )
        assert resp.status_code == 400

    def test_patch_req_task_not_found(self, client, test_requisition: Requisition):
        resp = client.patch(
            f"/api/requisitions/{test_requisition.id}/tasks/99999/status",
            json={"status": "done"},
        )
        assert resp.status_code == 404

    def test_patch_my_task_status(self, client, test_requisition: Requisition, test_user: User, db_session: Session):
        task = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Cross-req patch",
            assigned_to_id=test_user.id,
        )
        resp = client.patch(
            f"/api/tasks/{task.id}/status",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

    def test_patch_my_task_status_invalid(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Bad")
        resp = client.patch(
            f"/api/tasks/{task.id}/status",
            json={"status": "nope"},
        )
        assert resp.status_code == 400

    def test_patch_my_task_not_found(self, client):
        resp = client.patch(
            "/api/tasks/99999/status",
            json={"status": "done"},
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
