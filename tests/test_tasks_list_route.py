"""Tests for the filterable Tasks queue route — GET /v2/partials/tasks.

Covers: 200 + lists my open tasks; ?status= filters (done is excluded by default,
included when requested); ?priority= filters; ?due= buckets filter; another user's
task is excluded; the filter-bar (HX-Target=tasks-results) request returns the
results-only fragment (no filter bar); the filter bar carries an EXPLICIT hx-target.

Depends on: conftest.py (db_session, test_user, manager_user, test_requisition, client).
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.constants import TaskStatus
from app.models.task import RequisitionTask


def _add_task(db, *, req, user_id, title, status=TaskStatus.TODO.value, priority=2, due_at=None):
    t = RequisitionTask(
        requisition_id=req.id,
        title=title,
        status=status,
        priority=priority,
        assigned_to_id=user_id,
        due_at=due_at,
        completed_at=datetime.now(timezone.utc) if status == TaskStatus.DONE.value else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@pytest.fixture()
def my_open_task(db_session: Session, test_user, test_requisition) -> RequisitionTask:
    return _add_task(db_session, req=test_requisition, user_id=test_user.id, title="Open task alpha")


class TestTasksQueueRoute:
    def test_returns_200(self, client: TestClient):
        resp = client.get("/v2/partials/tasks")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_lists_my_open_task(self, client: TestClient, my_open_task):
        resp = client.get("/v2/partials/tasks")
        assert resp.status_code == 200
        assert my_open_task.title in resp.text

    def test_excludes_other_users_task(self, client: TestClient, db_session, manager_user, test_requisition):
        other = _add_task(db_session, req=test_requisition, user_id=manager_user.id, title="Not mine")
        resp = client.get("/v2/partials/tasks")
        assert other.title not in resp.text

    def test_default_excludes_done(self, client: TestClient, db_session, test_user, test_requisition):
        done = _add_task(
            db_session, req=test_requisition, user_id=test_user.id, title="Done task", status=TaskStatus.DONE.value
        )
        resp = client.get("/v2/partials/tasks")
        assert done.title not in resp.text

    def test_status_done_filter_shows_done(self, client: TestClient, db_session, test_user, test_requisition):
        done = _add_task(
            db_session, req=test_requisition, user_id=test_user.id, title="Done task", status=TaskStatus.DONE.value
        )
        open_t = _add_task(db_session, req=test_requisition, user_id=test_user.id, title="Open task")
        resp = client.get("/v2/partials/tasks?status=done")
        assert done.title in resp.text
        # status=done filters at the helper, so the open task should NOT appear.
        assert open_t.title not in resp.text

    def test_priority_filter(self, client: TestClient, db_session, test_user, test_requisition):
        high = _add_task(db_session, req=test_requisition, user_id=test_user.id, title="High task", priority=3)
        low = _add_task(db_session, req=test_requisition, user_id=test_user.id, title="Low task", priority=1)
        resp = client.get("/v2/partials/tasks?priority=3")
        assert high.title in resp.text
        assert low.title not in resp.text

    def test_due_overdue_filter(self, client: TestClient, db_session, test_user, test_requisition):
        overdue = _add_task(
            db_session,
            req=test_requisition,
            user_id=test_user.id,
            title="Overdue task",
            due_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        future = _add_task(
            db_session,
            req=test_requisition,
            user_id=test_user.id,
            title="Future task",
            due_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
        resp = client.get("/v2/partials/tasks?due=overdue")
        assert overdue.title in resp.text
        assert future.title not in resp.text

    def test_due_none_filter(self, client: TestClient, db_session, test_user, test_requisition):
        no_due = _add_task(db_session, req=test_requisition, user_id=test_user.id, title="No due task")
        with_due = _add_task(
            db_session,
            req=test_requisition,
            user_id=test_user.id,
            title="Has due task",
            due_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        resp = client.get("/v2/partials/tasks?due=none")
        assert no_due.title in resp.text
        assert with_due.title not in resp.text


class TestTasksQueueFilterBar:
    def test_full_load_has_filter_bar(self, client: TestClient):
        resp = client.get("/v2/partials/tasks")
        assert 'name="status"' in resp.text
        assert 'name="priority"' in resp.text
        assert 'name="due"' in resp.text

    def test_filter_bar_has_explicit_hx_target(self, client: TestClient):
        """The filter selects target the inner #tasks-results, never #main-content."""
        resp = client.get("/v2/partials/tasks")
        assert 'hx-target="#tasks-results"' in resp.text
        assert 'id="tasks-results"' in resp.text

    def test_results_only_fragment_on_hx_target_header(self, client: TestClient, my_open_task):
        """A filter-bar request (HX-Target=tasks-results) returns results only — no
        filter bar."""
        resp = client.get("/v2/partials/tasks", headers={"HX-Target": "tasks-results"})
        assert resp.status_code == 200
        assert my_open_task.title in resp.text
        # results-only: no filter selects, no page title swap
        assert 'name="status"' not in resp.text
        assert "<title" not in resp.text
