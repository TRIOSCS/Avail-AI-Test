"""Tests for the Tasks page (/v2/partials/my-day) — formerly "My Day".

The page was reworked from a follow-up-accounts + tasks split into a tasks-only,
filterable worklist (status / priority / due), grouped by urgency. The nav id stays
``my-day`` (URL/access unchanged); only the display label and the page became "Tasks".

Covers:
- route returns 200; heading + nav-level title say "Tasks"
- lists my open task assigned to me; excludes another user's task; excludes done by default
- the follow-up-accounts section is GONE (no overdue-account row, no outreach rail)
- urgency grouping headers (Overdue / Due soon / Later / No due date) render
- status / priority / due filters narrow the list
- the filter bar carries an EXPLICIT hx-target (#tasks-results) and a results-only
  fragment is returned for an HX-Target=tasks-results request
- completing a task from the page removes the row (outerHTML swap, empty fragment) and
  creates no ActivityLog

Called by: pytest
Depends on: conftest.py (db_session, test_user, manager_user, test_company,
            test_requisition, client)
"""

from __future__ import annotations

import os
import re

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.constants import TaskStatus
from app.models import ActivityLog, Company
from app.models.task import RequisitionTask


def _add_task(
    db: Session,
    *,
    user_id: int,
    title: str,
    company=None,
    req=None,
    status: str = TaskStatus.TODO.value,
    priority: int = 2,
    due_at=None,
) -> RequisitionTask:
    t = RequisitionTask(
        company_id=company.id if company is not None else None,
        requisition_id=req.id if req is not None else None,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def my_open_task(db_session: Session, test_user, test_company) -> RequisitionTask:
    """An open task assigned to test_user, linked to test_company, due yesterday."""
    return _add_task(
        db_session,
        user_id=test_user.id,
        title="Follow up on quote",
        company=test_company,
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


@pytest.fixture()
def other_user_task(db_session: Session, manager_user, test_company) -> RequisitionTask:
    """An open task assigned to another user — must NOT appear on my Tasks page."""
    return _add_task(
        db_session,
        user_id=manager_user.id,
        title="Other user task",
        company=test_company,
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


@pytest.fixture()
def my_done_task(db_session: Session, test_user, test_company) -> RequisitionTask:
    """A completed task assigned to test_user — excluded unless status=done."""
    return _add_task(
        db_session,
        user_id=test_user.id,
        title="Already done task",
        company=test_company,
        status=TaskStatus.DONE.value,
        due_at=datetime.now(timezone.utc) - timedelta(days=2),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTasksPageRoute:
    def test_returns_200(self, client: TestClient):
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_heading_and_title_say_tasks(self, client: TestClient):
        """The page heading and the OOB <title> both read 'Tasks', not 'My Day'."""
        resp = client.get("/v2/partials/my-day")
        assert ">Tasks<" in resp.text
        assert "Tasks — AvailAI" in resp.text
        assert "My Day" not in resp.text

    def test_shows_my_open_task(self, client: TestClient, my_open_task):
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert my_open_task.title in resp.text

    def test_excludes_other_users_task(self, client: TestClient, other_user_task, my_open_task):
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert other_user_task.title not in resp.text
        assert my_open_task.title in resp.text

    def test_default_excludes_done(self, client: TestClient, my_done_task):
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert my_done_task.title not in resp.text

    def test_full_page_returns_200(self, client: TestClient):
        """GET /v2/my-day full-page shell returns 200."""
        resp = client.get("/v2/my-day")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestTasksPageNoFollowUpSection:
    """The follow-up-accounts call-down section and its outreach rail are gone."""

    def test_no_follow_up_heading(self, client: TestClient, my_open_task):
        """The "Follow up" / "My tasks" section headings are gone.

        Matches the heading *element* (an <hN> whose inner text starts with the label
        after the opening ``>`` and whitespace) rather than a bare substring, so a task
        titled e.g. "Follow up on quote" (``>Follow up on quote``) can't false-positive.
        """
        resp = client.get("/v2/partials/my-day")
        assert not re.search(r">\s+Follow up\b", resp.text)
        assert not re.search(r">\s+My tasks\b", resp.text)

    def test_overdue_owned_account_not_listed(self, client: TestClient, db_session: Session, test_user):
        """An overdue account I own no longer surfaces here (it lives in CRM now)."""
        co = Company(
            name="Overdue Acme Inc",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=datetime.now(timezone.utc) - timedelta(days=40),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert co.name not in resp.text

    def test_no_outreach_rail(self, client: TestClient, my_open_task):
        """No Call/Email outreach affordance or auto-log hook on the Tasks page."""
        resp = client.get("/v2/partials/my-day")
        assert "tel:" not in resp.text
        assert "data-outreach-log" not in resp.text


class TestTasksPageGrouping:
    @freeze_time("2026-06-25 12:00:00")
    def test_urgency_group_headers_render(self, client: TestClient, db_session: Session, test_user, test_company):
        """Overdue / Due soon / Later / No due date headers appear for matching
        tasks."""
        now = datetime.now(timezone.utc)
        _add_task(
            db_session, user_id=test_user.id, title="Overdue one", company=test_company, due_at=now - timedelta(days=3)
        )
        _add_task(
            db_session, user_id=test_user.id, title="Soon one", company=test_company, due_at=now + timedelta(hours=2)
        )
        _add_task(
            db_session, user_id=test_user.id, title="Later one", company=test_company, due_at=now + timedelta(days=10)
        )
        _add_task(db_session, user_id=test_user.id, title="Undated one", company=test_company, due_at=None)
        resp = client.get("/v2/partials/my-day")
        assert "Overdue" in resp.text
        assert "Due soon" in resp.text
        assert "Later" in resp.text
        assert "No due date" in resp.text


class TestTasksPageDueTodayConsistency:
    """#4 — the 'Due today' filter and the results grouping agree.

    A task due earlier *today* renders under the 'Due soon' heading (labelled 'Due
    today'), never 'Overdue', and matches ``due=today`` (not ``due=overdue``). Frozen at
    18:00 UTC (14:00 US/Eastern) so the UTC and business-local calendar day coincide —
    this isolates the filter/grouping consistency from the display-timezone behaviour.
    Requests target the results-only fragment (no filter bar) so the string 'Overdue' can
    only originate from a group heading, not the filter <option>.
    """

    @freeze_time("2026-06-25 18:00:00")
    def test_due_earlier_today_renders_under_due_soon(self, client: TestClient, db_session, test_user, test_company):
        now = datetime.now(timezone.utc)
        _add_task(
            db_session,
            user_id=test_user.id,
            title="Due earlier today",
            company=test_company,
            due_at=now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ),  # midnight today — already "past" by the clock
        )
        resp = client.get("/v2/partials/my-day", headers={"HX-Target": "tasks-results"})
        assert resp.status_code == 200
        assert "Due earlier today" in resp.text
        assert "Due soon" in resp.text
        assert "Due today" in resp.text
        assert "Overdue" not in resp.text

    @freeze_time("2026-06-25 18:00:00")
    def test_due_today_filter_includes_earlier_today(self, client: TestClient, db_session, test_user, test_company):
        now = datetime.now(timezone.utc)
        earlier = _add_task(
            db_session,
            user_id=test_user.id,
            title="Nine AM task",
            company=test_company,
            due_at=now.replace(hour=9, minute=0, second=0, microsecond=0),
        )
        resp_today = client.get("/v2/partials/my-day?due=today", headers={"HX-Target": "tasks-results"})
        assert earlier.title in resp_today.text
        # The same task must NOT be caught by the overdue filter — no contradiction.
        resp_overdue = client.get("/v2/partials/my-day?due=overdue", headers={"HX-Target": "tasks-results"})
        assert earlier.title not in resp_overdue.text


class TestTasksPageBusinessTimezone:
    """#3 — due buckets use the business-local (US/Eastern) calendar day, not the UTC
    day.

    Frozen at 02:00 UTC on 2026-06-26, which is 22:00 US/Eastern on 2026-06-25. A task
    due 2026-06-25 is 'due today' for the business day, even though the UTC clock has
    rolled to the 26th (which the old UTC-day logic would have mislabelled 'Overdue').
    """

    @freeze_time("2026-06-26 02:00:00")
    def test_task_due_business_today_not_overdue_near_utc_midnight(
        self, client: TestClient, db_session, test_user, test_company
    ):
        _add_task(
            db_session,
            user_id=test_user.id,
            title="Due June 25 Eastern",
            company=test_company,
            due_at=datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc),
        )
        resp = client.get("/v2/partials/my-day", headers={"HX-Target": "tasks-results"})
        assert resp.status_code == 200
        assert "Due June 25 Eastern" in resp.text
        assert "Due today" in resp.text
        assert "Overdue" not in resp.text

    @freeze_time("2026-06-26 02:00:00")
    def test_due_today_filter_uses_business_day(self, client: TestClient, db_session, test_user, test_company):
        task = _add_task(
            db_session,
            user_id=test_user.id,
            title="Eastern today task",
            company=test_company,
            due_at=datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc),
        )
        resp_today = client.get("/v2/partials/my-day?due=today", headers={"HX-Target": "tasks-results"})
        assert task.title in resp_today.text
        resp_overdue = client.get("/v2/partials/my-day?due=overdue", headers={"HX-Target": "tasks-results"})
        assert task.title not in resp_overdue.text


class TestTasksPageFilters:
    def test_full_load_has_filter_bar(self, client: TestClient):
        resp = client.get("/v2/partials/my-day")
        assert 'name="status"' in resp.text
        assert 'name="priority"' in resp.text
        assert 'name="due"' in resp.text

    def test_filter_bar_has_explicit_hx_target(self, client: TestClient):
        """Filter selects target the inner #tasks-results, never #main-content."""
        resp = client.get("/v2/partials/my-day")
        assert 'hx-target="#tasks-results"' in resp.text
        assert 'id="tasks-results"' in resp.text

    def test_results_only_fragment_on_hx_target_header(self, client: TestClient, my_open_task):
        """A filter-bar request (HX-Target=tasks-results) returns results only — no
        filter bar, no title swap."""
        resp = client.get("/v2/partials/my-day", headers={"HX-Target": "tasks-results"})
        assert resp.status_code == 200
        assert my_open_task.title in resp.text
        assert 'name="status"' not in resp.text
        assert "<title" not in resp.text

    def test_status_done_filter_shows_done(self, client: TestClient, db_session, test_user, test_company):
        done = _add_task(
            db_session, user_id=test_user.id, title="Done task", company=test_company, status=TaskStatus.DONE.value
        )
        open_t = _add_task(db_session, user_id=test_user.id, title="Open task", company=test_company)
        resp = client.get("/v2/partials/my-day?status=done")
        assert done.title in resp.text
        assert open_t.title not in resp.text

    def test_priority_filter(self, client: TestClient, db_session, test_user, test_company):
        high = _add_task(db_session, user_id=test_user.id, title="High task", company=test_company, priority=3)
        low = _add_task(db_session, user_id=test_user.id, title="Low task", company=test_company, priority=1)
        resp = client.get("/v2/partials/my-day?priority=3")
        assert high.title in resp.text
        assert low.title not in resp.text

    def test_due_overdue_filter(self, client: TestClient, db_session, test_user, test_company):
        overdue = _add_task(
            db_session,
            user_id=test_user.id,
            title="Overdue task",
            company=test_company,
            due_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        future = _add_task(
            db_session,
            user_id=test_user.id,
            title="Future task",
            company=test_company,
            due_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
        resp = client.get("/v2/partials/my-day?due=overdue")
        assert overdue.title in resp.text
        assert future.title not in resp.text

    def test_due_upcoming_filter(self, client: TestClient, db_session, test_user, test_company):
        """A task due strictly in the future shows under due=upcoming (the 'Later'
        bucket) and is excluded from the mutually-exclusive today and overdue
        filters."""
        upcoming = _add_task(
            db_session,
            user_id=test_user.id,
            title="Upcoming task",
            company=test_company,
            due_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
        resp_upcoming = client.get("/v2/partials/my-day?due=upcoming", headers={"HX-Target": "tasks-results"})
        assert resp_upcoming.status_code == 200
        assert upcoming.title in resp_upcoming.text
        # The same future task must NOT match today or overdue — no contradiction.
        resp_today = client.get("/v2/partials/my-day?due=today", headers={"HX-Target": "tasks-results"})
        assert upcoming.title not in resp_today.text
        resp_overdue = client.get("/v2/partials/my-day?due=overdue", headers={"HX-Target": "tasks-results"})
        assert upcoming.title not in resp_overdue.text

    def test_due_none_filter(self, client: TestClient, db_session, test_user, test_company):
        no_due = _add_task(db_session, user_id=test_user.id, title="No due task", company=test_company)
        with_due = _add_task(
            db_session,
            user_id=test_user.id,
            title="Has due task",
            company=test_company,
            due_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        resp = client.get("/v2/partials/my-day?due=none")
        assert no_due.title in resp.text
        assert with_due.title not in resp.text


class TestTasksPageCompleteTask:
    def test_completing_task_returns_empty(self, client: TestClient, my_open_task):
        """POST complete with from_my_day=true returns empty fragment (row removes
        itself)."""
        resp = client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_completing_task_sets_done(self, client: TestClient, db_session: Session, my_open_task):
        client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        db_session.expire(my_open_task)
        assert my_open_task.status == "done"

    def test_completing_task_creates_no_activity_log(self, client: TestClient, db_session: Session, my_open_task):
        before = db_session.query(ActivityLog).count()
        client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        after = db_session.query(ActivityLog).count()
        assert after == before
