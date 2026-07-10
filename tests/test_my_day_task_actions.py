"""Tests for the Tasks-page (My Day) create / snooze / reopen affordances.

The Tasks page (/v2/partials/my-day) gained three row/page mutations (audit items L2/L3):
- Create — a personal/standalone to-do assigned to the creator. Standalone tasks have no
  natural business parent, but ck_task_has_parent requires one, so task_service hangs them
  off a per-user hidden "Personal" scratch requisition (excluded from every req list).
- Snooze — push an open task's due_at forward by a quick delta (+1d / +3d / +1w).
- Reopen — flip a done task back to todo and clear completed_at.

All three are gated by the same task-ownership authz the existing CRM/vendor task endpoints
use (task_service._is_crm_task_authorized: assignee / creator / account owner / admin).

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_company, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.constants import TaskStatus
from app.models import User
from app.models.sourcing import Requisition
from app.models.task import RequisitionTask


def _add_task(
    db: Session,
    *,
    user_id: int,
    title: str,
    company=None,
    created_by: int | None = None,
    status: str = TaskStatus.TODO.value,
    priority: int = 2,
    due_at=None,
) -> RequisitionTask:
    """Insert a company-scoped task assigned to a user (mirrors test_my_day helper)."""
    t = RequisitionTask(
        company_id=company.id if company is not None else None,
        title=title,
        status=status,
        priority=priority,
        assigned_to_id=user_id,
        created_by=created_by if created_by is not None else user_id,
        due_at=due_at,
        completed_at=datetime.now(UTC) if status == TaskStatus.DONE.value else None,
        created_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# A second buyer (has MY_DAY access, but owns none of test_user's tasks) so the
# authz 403 isolates the task-ownership gate, not module access.
# ---------------------------------------------------------------------------


@pytest.fixture()
def other_user(db_session: Session) -> User:
    u = User(
        email="otherbuyer@trioscs.com",
        name="Other Buyer",
        role="buyer",
        azure_id="test-azure-id-other-buyer",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_client(db_session: Session, other_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return other_user

    async def _override_token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_token
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ===========================================================================
# Create — personal / standalone task
# ===========================================================================


class TestCreatePersonalTaskService:
    def test_create_uses_hidden_scratch_parent(self, db_session: Session, test_user: User):
        from app.services.task_service import create_personal_task

        t = create_personal_task(db_session, user_id=test_user.id, title="Solo todo", priority=1)
        assert t.assigned_to_id == test_user.id
        assert t.created_by == test_user.id
        assert t.task_type == "general"
        assert t.status == TaskStatus.TODO.value
        req = db_session.get(Requisition, t.requisition_id)
        assert req is not None
        assert req.is_scratch is True
        assert req.created_by == test_user.id

    def test_personal_requisition_is_idempotent(self, db_session: Session, test_user: User):
        from app.services.task_service import get_or_create_personal_requisition

        r1 = get_or_create_personal_requisition(db_session, test_user.id)
        r2 = get_or_create_personal_requisition(db_session, test_user.id)
        assert r1.id == r2.id


class TestCreatePersonalTaskRoute:
    def test_create_appears_in_list(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post("/v2/partials/my-day/tasks", data={"title": "Call the vendor back"})
        assert resp.status_code == 200
        assert "Call the vendor back" in resp.text
        task = db_session.query(RequisitionTask).filter_by(title="Call the vendor back").first()
        assert task is not None
        assert task.assigned_to_id == test_user.id
        assert task.status == TaskStatus.TODO.value

    def test_create_requires_title(self, client: TestClient):
        resp = client.post("/v2/partials/my-day/tasks", data={"title": "   "})
        assert resp.status_code == 422

    def test_create_parses_due_and_priority(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/my-day/tasks",
            data={"title": "Priority task", "due_at": "2026-08-01", "task_priority": "3"},
        )
        assert resp.status_code == 200
        task = db_session.query(RequisitionTask).filter_by(title="Priority task").first()
        assert task.priority == 3
        assert task.due_at is not None
        assert task.due_at.tzinfo is not None  # aware, not a raw string
        assert (task.due_at.year, task.due_at.month, task.due_at.day) == (2026, 8, 1)
        assert task.due_at.hour == 0  # normalized to UTC midnight

    def test_personal_task_has_no_requisition_link(self, client: TestClient):
        """The scratch parent is hidden, so the row renders no requisition link."""
        client.post("/v2/partials/my-day/tasks", data={"title": "Unlinked todo"})
        resp = client.get("/v2/partials/my-day")
        assert "Unlinked todo" in resp.text
        assert "/v2/partials/requisitions/" not in resp.text


# ===========================================================================
# Snooze
# ===========================================================================


class TestSnoozeService:
    def test_snooze_days_advances_by_delta(self, db_session: Session, test_user: User, test_company):
        from app.services.task_service import snooze_task

        due = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        t = _add_task(db_session, user_id=test_user.id, company=test_company, title="Snz1", due_at=due)
        snoozed = snooze_task(db_session, t.id, days=1)
        assert abs((snoozed.due_at - due).total_seconds() - 86400) < 2

    def test_snooze_default_still_one_week(self, db_session: Session, test_user: User, test_company):
        """Regression: the CRM/vendor Snooze contract (no days arg → +1 week) is unchanged."""
        from app.services.task_service import snooze_task

        due = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        t = _add_task(db_session, user_id=test_user.id, company=test_company, title="Snz2", due_at=due)
        snoozed = snooze_task(db_session, t.id)
        assert abs((snoozed.due_at - (due + timedelta(weeks=1))).total_seconds()) < 2


class TestSnoozeRoute:
    @pytest.mark.parametrize("days", [1, 3, 7])
    def test_snooze_route_advances_due_at(
        self, days, client: TestClient, db_session: Session, test_user: User, test_company
    ):
        due = datetime.now(UTC) + timedelta(days=2)
        t = _add_task(db_session, user_id=test_user.id, company=test_company, title="Snz route", due_at=due)
        resp = client.post(f"/v2/partials/my-day/tasks/{t.id}/snooze?days={days}")
        assert resp.status_code == 200
        db_session.refresh(t)
        assert abs((t.due_at - (due + timedelta(days=days))).total_seconds()) < 2

    def test_snooze_unauthorized_returns_403(
        self, other_client: TestClient, db_session: Session, test_user: User, test_company
    ):
        t = _add_task(
            db_session,
            user_id=test_user.id,
            company=test_company,
            title="Not yours",
            due_at=datetime.now(UTC) + timedelta(days=1),
        )
        resp = other_client.post(f"/v2/partials/my-day/tasks/{t.id}/snooze?days=1")
        assert resp.status_code == 403

    def test_snooze_unknown_task_returns_404(self, client: TestClient):
        resp = client.post("/v2/partials/my-day/tasks/999999/snooze?days=1")
        assert resp.status_code == 404


# ===========================================================================
# Reopen
# ===========================================================================


class TestReopenRoute:
    def test_reopen_flips_done_to_todo_and_clears_completed_at(
        self, client: TestClient, db_session: Session, test_user: User, test_company
    ):
        t = _add_task(
            db_session,
            user_id=test_user.id,
            company=test_company,
            title="Reopen me",
            status=TaskStatus.DONE.value,
        )
        assert t.completed_at is not None
        resp = client.post(f"/v2/partials/my-day/tasks/{t.id}/reopen")
        assert resp.status_code == 200
        db_session.refresh(t)
        assert t.status == TaskStatus.TODO.value
        assert t.completed_at is None

    def test_reopen_from_done_view_removes_row(
        self, client: TestClient, db_session: Session, test_user: User, test_company
    ):
        """Reopening while the Done filter is active re-renders without the (now todo)
        row."""
        t = _add_task(
            db_session,
            user_id=test_user.id,
            company=test_company,
            title="WasDone",
            status=TaskStatus.DONE.value,
        )
        resp = client.post(f"/v2/partials/my-day/tasks/{t.id}/reopen", data={"status": "done"})
        assert resp.status_code == 200
        assert "WasDone" not in resp.text

    def test_reopen_unauthorized_returns_403(
        self, other_client: TestClient, db_session: Session, test_user: User, test_company
    ):
        t = _add_task(
            db_session,
            user_id=test_user.id,
            company=test_company,
            title="Not yours done",
            status=TaskStatus.DONE.value,
        )
        resp = other_client.post(f"/v2/partials/my-day/tasks/{t.id}/reopen")
        assert resp.status_code == 403

    def test_reopen_unknown_task_returns_404(self, client: TestClient):
        resp = client.post("/v2/partials/my-day/tasks/999999/reopen")
        assert resp.status_code == 404
