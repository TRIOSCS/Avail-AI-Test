"""Tests for the requisition Task-board mutation endpoints (finding C1).

Covers the create/complete/delete endpoints that back the requisition detail "Tasks"
tab (requisitions/tabs/tasks.html), which previously POSTed to non-existent
/api/requisitions/{id}/tasks* routes (every button 404'd silently):

- create with the board's full field set (title + type + priority + assignee + due)
- due-date is parsed to an aware UTC datetime, never bound as a raw string (finding H2
  trap: UTCDateTime passes strings through unnormalized)
- create clears the empty state and re-renders the list body
- missing title → 422
- complete flips a task to done (works for unassigned / other-assigned board tasks)
- delete removes the task
- IDOR: completing/deleting a task that belongs to a different requisition → 404
- authz: a restricted-role user who does not own the requisition → 404 on every mutation

Called by: pytest
Depends on: conftest.py (db_session, test_user, sales_user, test_requisition, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import Requisition, User
from app.models.task import RequisitionTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def existing_task(db_session: Session, test_requisition: Requisition) -> RequisitionTask:
    """A todo task on the requisition, unassigned (board tasks need not be assigned)."""
    t = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Review incoming offers",
        task_type="sourcing",
        status=TaskStatus.TODO,
        priority=2,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def other_requisition(db_session: Session, test_user: User) -> Requisition:
    """A second requisition, used to prove the IDOR guard on complete/delete."""
    req = Requisition(
        name="REQ-TEST-002",
        customer_name="Beta Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def sales_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient authenticated as a restricted (sales) user who owns no requisition."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    async def _override_fresh_token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Tab render (full include chain: tasks.html -> _task_list.html -> _task_row.html)
# ---------------------------------------------------------------------------


class TestTaskTabRender:
    def test_tab_renders_row_and_wired_buttons(
        self, client, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/tasks")
        assert resp.status_code == 200
        html = resp.text
        assert existing_task.title in html
        # the extracted row partial rendered with the group class (hover delete reachable)
        assert "group flex items-center" in html
        # create form now swaps innerHTML, and the row's mutation buttons point at the
        # real endpoints
        assert 'hx-swap="innerHTML"' in html
        assert f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}/complete" in html
        assert f'hx-delete="/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}"' in html

    def test_empty_tab_shows_empty_state(self, client, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/tasks")
        assert resp.status_code == 200
        assert "No tasks yet" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateRequisitionTask:
    def test_create_with_type_priority_assignee_and_due(
        self, client, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={
                "title": "Cut PO for LM317T",
                "task_type": "sourcing",
                "priority": "3",
                "assigned_to_id": str(test_user.id),
                "due_at": "2026-07-10",
            },
        )
        assert resp.status_code == 200
        assert "Cut PO for LM317T" in resp.text

        task = (
            db_session.query(RequisitionTask)
            .filter(RequisitionTask.requisition_id == test_requisition.id)
            .order_by(RequisitionTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.title == "Cut PO for LM317T"
        assert task.task_type == "sourcing"
        assert task.priority == 3
        assert task.assigned_to_id == test_user.id
        assert task.created_by == test_user.id
        assert task.source == "manual"
        # requisition-scoped, NOT part-scoped
        assert task.requisition_id == test_requisition.id
        assert task.requirement_id is None

    def test_due_date_stored_as_aware_datetime_not_string(
        self, client, db_session: Session, test_requisition: Requisition
    ):
        """The H2 trap: a raw 'YYYY-MM-DD' string bound to the timestamptz column would
        round-trip as a string (AttributeError on .strftime). The endpoint must parse it
        to an aware UTC-midnight datetime."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "With due date", "due_at": "2026-07-10"},
        )
        assert resp.status_code == 200
        task = db_session.query(RequisitionTask).filter(RequisitionTask.title == "With due date").first()
        assert isinstance(task.due_at, datetime)
        assert task.due_at.tzinfo is not None
        assert task.due_at.date() == date(2026, 7, 10)
        # renders without raising (the string bug would blow up here)
        assert task.due_at.strftime("%b %d") == "Jul 10"

    def test_create_without_due_date_is_none(self, client, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "No due date", "due_at": ""},
        )
        assert resp.status_code == 200
        task = db_session.query(RequisitionTask).filter(RequisitionTask.title == "No due date").first()
        assert task.due_at is None

    def test_create_defaults_type_and_priority(self, client, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "Bare task"},
        )
        assert resp.status_code == 200
        task = db_session.query(RequisitionTask).filter(RequisitionTask.title == "Bare task").first()
        assert task.task_type == "general"
        assert task.priority == 2
        assert task.assigned_to_id is None

    def test_create_first_task_clears_empty_state(self, client, test_requisition: Requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "First task ever"},
        )
        assert resp.status_code == 200
        assert "First task ever" in resp.text
        assert "No tasks yet" not in resp.text

    def test_create_missing_title_422(self, client, test_requisition: Requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "   "},
        )
        assert resp.status_code == 422

    def test_create_unknown_requisition_404(self, client):
        resp = client.post("/api/requisitions/999999/tasks", data={"title": "Ghost"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class TestCompleteRequisitionTask:
    def test_complete_marks_done(
        self, client, db_session: Session, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}/complete")
        assert resp.status_code == 200
        # completed row renders with the strike-through title
        assert "line-through" in resp.text
        db_session.expire_all()
        refreshed = db_session.get(RequisitionTask, existing_task.id)
        assert refreshed.status == TaskStatus.DONE
        assert refreshed.completed_at is not None

    def test_complete_works_for_unassigned_board_task(
        self, client, db_session: Session, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        """The shared board must complete tasks even when assigned_to_id is None (the
        assignee-only part-comms path would reject these)."""
        assert existing_task.assigned_to_id is None
        resp = client.post(f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}/complete")
        assert resp.status_code == 200

    def test_complete_task_from_other_requisition_404(
        self,
        client,
        db_session: Session,
        other_requisition: Requisition,
        existing_task: RequisitionTask,
    ):
        """IDOR: task belongs to test_requisition, URL names other_requisition → 404."""
        resp = client.post(f"/api/requisitions/{other_requisition.id}/tasks/{existing_task.id}/complete")
        assert resp.status_code == 404

    def test_complete_unknown_task_404(self, client, test_requisition: Requisition):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/tasks/999999/complete")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteRequisitionTask:
    def test_delete_removes_task(
        self, client, db_session: Session, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        resp = client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}")
        assert resp.status_code == 200
        assert resp.text == ""
        db_session.expire_all()
        assert db_session.get(RequisitionTask, existing_task.id) is None

    def test_delete_task_from_other_requisition_404(
        self,
        client,
        db_session: Session,
        other_requisition: Requisition,
        existing_task: RequisitionTask,
    ):
        resp = client.delete(f"/api/requisitions/{other_requisition.id}/tasks/{existing_task.id}")
        assert resp.status_code == 404
        # not deleted
        assert db_session.get(RequisitionTask, existing_task.id) is not None

    def test_delete_unknown_task_404(self, client, test_requisition: Requisition):
        resp = client.delete(f"/api/requisitions/{test_requisition.id}/tasks/999999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Authz — restricted role that does not own the requisition
# ---------------------------------------------------------------------------


class TestRequisitionTaskAuthz:
    """A restricted (sales) user who is not the requisition owner is denied (404, so
    existence isn't leaked) on every mutation."""

    def test_restricted_user_cannot_create(self, sales_client, test_requisition: Requisition):
        resp = sales_client.post(
            f"/api/requisitions/{test_requisition.id}/tasks",
            data={"title": "Injected task"},
        )
        assert resp.status_code == 404

    def test_restricted_user_cannot_complete(
        self, sales_client, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        resp = sales_client.post(f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}/complete")
        assert resp.status_code == 404

    def test_restricted_user_cannot_delete(
        self, sales_client, test_requisition: Requisition, existing_task: RequisitionTask
    ):
        resp = sales_client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{existing_task.id}")
        assert resp.status_code == 404
