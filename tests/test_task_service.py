"""
test_task_service.py — Tests for the Requisition Task Board

Tests CRUD operations, auto-generation, auto-close, and API endpoints
for the pipeline task board.

Depends on: conftest.py fixtures, app/services/task_service.py, app/routers/task.py
"""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.services import task_service

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

    def test_get_my_tasks_summary(self, db_session: Session, test_user: User, test_requisition: Requisition):
        task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Todo",
            assigned_to_id=test_user.id,
        )
        t2 = task_service.create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="In prog",
            assigned_to_id=test_user.id,
        )
        task_service.update_task_status(db_session, t2.id, "in_progress")
        summary = task_service.get_my_tasks_summary(db_session, test_user.id)
        assert summary["todo"] == 1
        assert summary["in_progress"] == 1
        assert summary["total"] == 2


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


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestTaskAPI:
    def test_list_tasks(self, client, test_requisition: Requisition, db_session: Session):
        task_service.create_task(db_session, requisition_id=test_requisition.id, title="API task")
        # SQLite strips timezone from created_at, causing naive-vs-aware comparison
        # in apply_simple_scoring. Patch it to avoid the SQLite-specific issue.
        with patch("app.routers.task.task_service.apply_simple_scoring"):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "API task"

    def test_create_task_api(self, client, test_requisition: Requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            json={"title": "New API task", "task_type": "sourcing", "priority": 3},
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "New API task"
        assert resp.json()["task_type"] == "sourcing"

    def test_update_task_api(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Update me")
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}",
            json={"title": "Updated via API"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated via API"

    def test_update_status_api(self, client, test_requisition: Requisition, db_session: Session):
        task = task_service.create_task(db_session, requisition_id=test_requisition.id, title="Status test")
        resp = client.patch(
            f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/status",
            json={"status": "done"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
        assert resp.json()["completed_at"] is not None

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
        assert data["todo"] == 1
        assert data["total"] == 1

    def test_task_not_found(self, client, test_requisition: Requisition):
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/tasks/99999",
            json={"title": "Ghost"},
        )
        assert resp.status_code == 404
