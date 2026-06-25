"""Tests for Step 6 — My Day worklist (/v2/partials/my-day).

Covers:
- route returns 200 with overdue account I own + open task assigned to me
- does NOT list another user's overdue account (my_only scoping)
- does NOT list another user's task
- completed task does not appear
- on-target account (not overdue) does not appear
- empty state renders when nothing is due
- completing a task from My Day removes the row (outerHTML swap, empty fragment)
- completing a task from My Day does NOT create an ActivityLog

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_company, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.models import ActivityLog, Company
from app.models.crm import CustomerSite, SiteContact
from app.models.task import RequisitionTask


def _add_primary_contact(
    db_session: Session,
    company: Company,
    *,
    phone: str = "+15551234567",
    email: str = "buyer@acme.test",
    do_not_contact: bool = False,
) -> SiteContact:
    """Attach a primary SiteContact (via a CustomerSite) to ``company``.

    Used to exercise the My Day one-click outreach rail (Call / Email), which reads
    ``company.primary_contact``.
    """
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Pat Buyer",
        phone=phone,
        email=email,
        do_not_contact=do_not_contact,
    )
    db_session.add(contact)
    db_session.flush()
    company.primary_contact_id = contact.id
    db_session.commit()
    db_session.refresh(company)
    return contact


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def overdue_owned_company(db_session: Session, test_user) -> Company:
    """An account owned by test_user whose outbound clock is 35 days stale (overdue)."""
    co = Company(
        name="Overdue Acme",
        is_active=True,
        account_owner_id=test_user.id,
        last_outbound_at=datetime.now(timezone.utc) - timedelta(days=35),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def other_user(db_session: Session):
    """A second user — used to verify my_only scoping."""
    from app.models.auth import User

    u = User(
        email="other@trioscs.com",
        name="Other User",
        role="buyer",
        azure_id="test-azure-id-other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_user_overdue_company(db_session: Session, other_user) -> Company:
    """An overdue account owned by other_user — must NOT appear on my My Day."""
    co = Company(
        name="Other User Overdue",
        is_active=True,
        account_owner_id=other_user.id,
        last_outbound_at=datetime.now(timezone.utc) - timedelta(days=40),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def on_target_owned_company(db_session: Session, test_user) -> Company:
    """An account owned by test_user with a recent outbound (2 days ago — on target)."""
    co = Company(
        name="On Target Corp",
        is_active=True,
        account_owner_id=test_user.id,
        last_outbound_at=datetime.now(timezone.utc) - timedelta(days=2),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def my_open_task(db_session: Session, test_user, test_company) -> RequisitionTask:
    """An open task assigned to test_user, linked to test_company, due yesterday."""
    t = RequisitionTask(
        company_id=test_company.id,
        title="Follow up on quote",
        status="todo",
        assigned_to_id=test_user.id,
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def other_user_task(db_session: Session, other_user, test_company) -> RequisitionTask:
    """An open task assigned to other_user — must NOT appear on my My Day."""
    t = RequisitionTask(
        company_id=test_company.id,
        title="Other user task",
        status="todo",
        assigned_to_id=other_user.id,
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def my_done_task(db_session: Session, test_user, test_company) -> RequisitionTask:
    """A completed task assigned to test_user — must NOT appear."""
    t = RequisitionTask(
        company_id=test_company.id,
        title="Already done task",
        status="done",
        assigned_to_id=test_user.id,
        due_at=datetime.now(timezone.utc) - timedelta(days=2),
        completed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMyDayRoute:
    def test_returns_200(self, client: TestClient):
        """Route returns 200 HTML."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_shows_overdue_account_i_own(
        self, client: TestClient, db_session: Session, test_user, overdue_owned_company
    ):
        """Overdue account I own appears in the Follow up section."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert overdue_owned_company.name in resp.text

    def test_shows_my_open_task(self, client: TestClient, db_session: Session, my_open_task):
        """Open task assigned to me appears in the My tasks section."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert my_open_task.title in resp.text

    def test_excludes_other_users_account(
        self,
        client: TestClient,
        db_session: Session,
        other_user_overdue_company,
        overdue_owned_company,
    ):
        """Overdue account owned by another user does NOT appear (my_only scoping)."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert other_user_overdue_company.name not in resp.text
        # Own account still there
        assert overdue_owned_company.name in resp.text

    def test_excludes_other_users_task(
        self,
        client: TestClient,
        db_session: Session,
        other_user_task,
        my_open_task,
    ):
        """Task assigned to another user does NOT appear."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert other_user_task.title not in resp.text
        assert my_open_task.title in resp.text

    def test_excludes_completed_task(self, client: TestClient, db_session: Session, my_done_task):
        """Completed task does not appear."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert my_done_task.title not in resp.text

    def test_excludes_on_target_account(self, client: TestClient, db_session: Session, on_target_owned_company):
        """Account with recent outbound (on target) does not appear in Follow up."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert on_target_owned_company.name not in resp.text

    def test_empty_state_when_nothing_due(self, client: TestClient, db_session: Session):
        """Empty state renders when no overdue accounts and no open tasks."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "All caught up" in resp.text

    def test_empty_state_absent_when_there_is_work(self, client: TestClient, db_session: Session, my_open_task):
        """Empty state does not show when there is at least one open task."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "All caught up" not in resp.text


class TestMyDayCompleteTask:
    def test_completing_task_from_my_day_returns_empty(self, client: TestClient, db_session: Session, my_open_task):
        """POST complete with from_my_day=true returns empty fragment (row removes
        itself)."""
        resp = client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_completing_task_from_my_day_sets_done(self, client: TestClient, db_session: Session, my_open_task):
        """Task status becomes done after completing from My Day."""
        client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        db_session.expire(my_open_task)
        assert my_open_task.status == "done"

    def test_completing_task_creates_no_activity_log(self, client: TestClient, db_session: Session, my_open_task):
        """Completing a task from My Day creates NO ActivityLog (no fake logging)."""
        before = db_session.query(ActivityLog).count()
        client.post(f"/v2/partials/tasks/{my_open_task.id}/complete?from_my_day=true")
        after = db_session.query(ActivityLog).count()
        assert after == before


class TestMyDayAttentionHeader:
    def test_attention_count_shown_for_overdue_account(
        self, client: TestClient, db_session: Session, overdue_owned_company
    ):
        """The header surfaces a 'needs a call today' attention figure for an overdue
        account."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "need" in resp.text and "a call today" in resp.text
        # The accent key-figure container is present (figure-accent token).
        assert "figure-accent" in resp.text

    def test_no_attention_figure_when_no_accounts(self, client: TestClient, db_session: Session, my_open_task):
        """With tasks but no overdue accounts, the call-today figure is absent."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "a call today" not in resp.text


class TestMyDayOutreach:
    def test_call_and_email_actions_render_for_reachable_contact(
        self, client: TestClient, db_session: Session, overdue_owned_company
    ):
        """A reachable primary contact yields one-click Call (tel:) + Email links."""
        _add_primary_contact(db_session, overdue_owned_company)
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "tel:+15551234567" in resp.text
        assert "buyer@acme.test" in resp.text
        # Outreach auto-logging hook is wired on the action.
        assert "data-outreach-log" in resp.text

    def test_outreach_suppressed_for_do_not_contact(
        self, client: TestClient, db_session: Session, overdue_owned_company
    ):
        """A do_not_contact primary contact gets no Call/Email affordance."""
        _add_primary_contact(db_session, overdue_owned_company, do_not_contact=True)
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert "tel:+15551234567" not in resp.text
        assert "buyer@acme.test" not in resp.text

    def test_no_call_link_when_account_has_no_contact(
        self, client: TestClient, db_session: Session, overdue_owned_company
    ):
        """An overdue account with no primary contact still renders (no tel: link), and
        the row's Log action remains available."""
        resp = client.get("/v2/partials/my-day")
        assert resp.status_code == 200
        assert overdue_owned_company.name in resp.text
        assert "tel:" not in resp.text
        # The manual log affordance is always present on a follow-up row.
        assert "activity/add-note-form" in resp.text


class TestMyDayFullPage:
    def test_full_page_returns_200(self, client: TestClient):
        """GET /v2/my-day full-page shell returns 200."""
        resp = client.get("/v2/my-day")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
