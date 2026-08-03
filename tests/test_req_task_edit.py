"""Requisition-board task edit (title + due) — Task 4 of the 2026-08-03 plan.

Covers the three new board endpoints (GET .../row, GET .../edit-form,
POST .../edit): form render for an authorized user, 403 for an
uninvolved user, save (title + due, empty due clears), empty-title
validation re-render, IDOR 404, and the pencil button wiring in
_task_row.html.

Called by: pytest
Depends on: conftest.py (db_session, client, test_user, admin_user,
    test_requisition), app.models.task
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, date, datetime

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
def board_task(db_session: Session, test_requisition: Requisition, test_user: User) -> RequisitionTask:
    """A board task created by and assigned to test_user (the `client` identity)."""
    t = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Original title",
        task_type="general",
        status=TaskStatus.TODO,
        priority=2,
        source="manual",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def other_requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="REQ-EDIT-IDOR",
        customer_name="Beta Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def stranger_client(db_session: Session):
    """Client for a second unrestricted (buyer) user uninvolved with any task."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="edit-stranger@trioscs.com",
        name="Edit Stranger",
        role="buyer",
        azure_id="test-azure-id-edit-stranger",
        created_at=datetime.now(UTC),
    )
    db_session.add(stranger)
    db_session.commit()
    db_session.refresh(stranger)

    def _override_db():
        yield db_session

    def _override_user():
        return stranger

    async def _override_fresh():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Edit form
# ---------------------------------------------------------------------------


class TestEditForm:
    def test_creator_gets_edit_form(self, client, test_requisition: Requisition, board_task: RequisitionTask):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit-form")
        assert resp.status_code == 200
        assert 'name="title"' in resp.text
        assert 'name="due_at"' in resp.text
        assert f'id="task-{board_task.id}"' in resp.text

    def test_uninvolved_user_gets_403(
        self, stranger_client, test_requisition: Requisition, board_task: RequisitionTask
    ):
        resp = stranger_client.get(f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit-form")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class TestEditSave:
    def test_save_updates_title_and_due(
        self, client, db_session: Session, test_requisition: Requisition, board_task: RequisitionTask
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit",
            data={"title": "Renamed task", "due_at": "2026-09-15"},
        )
        assert resp.status_code == 200
        assert "Renamed task" in resp.text
        db_session.expire_all()
        refreshed = db_session.get(RequisitionTask, board_task.id)
        assert refreshed.title == "Renamed task"
        assert refreshed.due_at is not None
        assert refreshed.due_at.date() == date(2026, 9, 15)

    def test_save_empty_due_clears(
        self, client, db_session: Session, test_requisition: Requisition, board_task: RequisitionTask
    ):
        board_task.due_at = datetime(2026, 9, 1, tzinfo=UTC)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit",
            data={"title": "Still here", "due_at": ""},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(RequisitionTask, board_task.id).due_at is None

    def test_save_empty_title_rerenders_form_with_error(
        self, client, db_session: Session, test_requisition: Requisition, board_task: RequisitionTask
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit",
            data={"title": "   ", "due_at": ""},
        )
        assert resp.status_code == 200
        assert "Title is required." in resp.text
        assert 'name="title"' in resp.text
        db_session.expire_all()
        assert db_session.get(RequisitionTask, board_task.id).title == "Original title"

    def test_uninvolved_user_cannot_save(
        self, stranger_client, db_session: Session, test_requisition: Requisition, board_task: RequisitionTask
    ):
        resp = stranger_client.post(
            f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit",
            data={"title": "Hijacked", "due_at": ""},
        )
        assert resp.status_code == 403
        db_session.expire_all()
        assert db_session.get(RequisitionTask, board_task.id).title == "Original title"

    def test_idor_task_from_other_requisition_404(
        self, client, other_requisition: Requisition, board_task: RequisitionTask
    ):
        resp = client.post(
            f"/api/requisitions/{other_requisition.id}/tasks/{board_task.id}/edit",
            data={"title": "IDOR", "due_at": ""},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cancel (row re-render) + pencil wiring
# ---------------------------------------------------------------------------


class TestRowAndPencil:
    def test_row_endpoint_renders_row(self, client, test_requisition: Requisition, board_task: RequisitionTask):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/row")
        assert resp.status_code == 200
        assert f'id="task-{board_task.id}"' in resp.text
        assert "Original title" in resp.text

    def test_task_row_has_pencil_button(self, client, test_requisition: Requisition, board_task: RequisitionTask):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/tasks")
        assert resp.status_code == 200
        assert f"/api/requisitions/{test_requisition.id}/tasks/{board_task.id}/edit-form" in resp.text
