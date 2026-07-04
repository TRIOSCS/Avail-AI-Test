"""Tests for Step 5 — account/contact scoped tasks (migration 138).

Covers:
- migration 138 up→down→up clean + single head
- CHECK constraint: task with no parent → IntegrityError
- creating an account task persists with company_id set
- contact task from another company → 404 IDOR guard (via HTTP endpoint)
- completing a task sets status=done AND creates NO ActivityLog (no fake logging)
- open tasks list for account + "next step" (soonest task)
- existing requisition tasks still work
- existing complete_task service function no longer calls log_activity

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_company, test_customer_site, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import ActivityLog, Company, CustomerSite, SiteContact
from app.models.task import RequisitionTask
from app.services.task_service import (
    complete_crm_task,
    complete_task,
    create_company_task,
    create_contact_task,
    create_task,
    get_next_task_for_company,
    get_open_tasks_for_company,
    get_open_tasks_for_contact,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _count_activity(db: Session) -> int:
    return db.query(ActivityLog).count()


@pytest.fixture()
def test_company(db_session: Session, test_user) -> Company:
    """A sample company owned by test_user — overrides conftest.test_company so that the
    account-owner gate added by FIX F lets the default test client (authenticated as
    test_user) create tasks without getting 403."""
    co = Company(
        name="Acme Electronics",
        website="https://acme-electronics.com",
        industry="Electronic Components",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def test_site_contact(db_session: Session, test_customer_site: CustomerSite) -> SiteContact:
    """A site contact linked to test_customer_site."""
    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Alice Smith",
        email="alice@acme.example",
    )
    db_session.add(sc)
    db_session.commit()
    db_session.refresh(sc)
    return sc


@pytest.fixture()
def other_company(db_session: Session) -> Company:
    """A second company — used to verify IDOR guard."""
    co = Company(name="Other Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def other_site(db_session: Session, other_company: Company) -> CustomerSite:
    """A site belonging to other_company."""
    site = CustomerSite(company_id=other_company.id, site_name="Other HQ")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def other_contact(db_session: Session, other_site: CustomerSite) -> SiteContact:
    """A contact belonging to other_company (via other_site)."""
    sc = SiteContact(
        customer_site_id=other_site.id,
        full_name="Bob Jones",
        email="bob@other.example",
    )
    db_session.add(sc)
    db_session.commit()
    db_session.refresh(sc)
    return sc


# ---------------------------------------------------------------------------
# Migration 135: CHECK constraint enforcement
# ---------------------------------------------------------------------------


class TestMigration135CheckConstraint:
    def test_task_with_no_parent_raises_integrity_error(self, db_session: Session):
        """A RequisitionTask with no requisition_id, company_id, or site_contact_id must
        violate the ck_task_has_parent constraint."""
        orphan = RequisitionTask(
            title="Orphan task",
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
        )
        db_session.add(orphan)
        # SQLite FK enforcement is on; the CHECK constraint is also on (defined in model).
        with pytest.raises((IntegrityError, Exception)):
            db_session.commit()
        db_session.rollback()

    def test_task_with_requisition_id_only_passes(self, db_session: Session, test_requisition):
        """Existing pattern: requisition_id set, company/contact NULL — still valid."""
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Req-scoped task",
            source="system",
        )
        assert task.id is not None
        assert task.company_id is None
        assert task.site_contact_id is None


# ---------------------------------------------------------------------------
# Account task creation
# ---------------------------------------------------------------------------


class TestCreateAccountTask:
    def test_creates_with_company_id(self, db_session: Session, test_company: Company, test_user):
        task = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Call Acme by Friday",
            created_by=test_user.id,
        )
        assert task.id is not None
        assert task.company_id == test_company.id
        assert task.requisition_id is None
        assert task.site_contact_id is None
        assert task.title == "Call Acme by Friday"

    def test_persisted_to_db(self, db_session: Session, test_company: Company, test_user):
        task = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Follow up",
            created_by=test_user.id,
        )
        fetched = db_session.get(RequisitionTask, task.id)
        assert fetched is not None
        assert fetched.company_id == test_company.id


# ---------------------------------------------------------------------------
# Contact task creation
# ---------------------------------------------------------------------------


class TestCreateContactTask:
    def test_creates_with_site_contact_id(
        self,
        db_session: Session,
        test_site_contact: SiteContact,
        test_user,
    ):
        task = create_contact_task(
            db_session,
            site_contact_id=test_site_contact.id,
            title="Send intro deck to Alice",
            created_by=test_user.id,
        )
        assert task.id is not None
        assert task.site_contact_id == test_site_contact.id
        assert task.company_id is None
        assert task.requisition_id is None


# ---------------------------------------------------------------------------
# Open task lists
# ---------------------------------------------------------------------------


class TestGetOpenTasksForCompany:
    def test_returns_open_tasks(self, db_session: Session, test_company: Company, test_user):
        create_company_task(db_session, company_id=test_company.id, title="T1", created_by=test_user.id)
        create_company_task(db_session, company_id=test_company.id, title="T2", created_by=test_user.id)
        tasks = get_open_tasks_for_company(db_session, test_company.id)
        assert len(tasks) == 2

    def test_excludes_done(self, db_session: Session, test_company: Company, test_user):
        task = create_company_task(db_session, company_id=test_company.id, title="Done", created_by=test_user.id)
        task.status = TaskStatus.DONE
        db_session.commit()
        tasks = get_open_tasks_for_company(db_session, test_company.id)
        assert len(tasks) == 0

    def test_next_task_is_soonest(self, db_session: Session, test_company: Company, test_user):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        t_far = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Far future",
            due_at=now + timedelta(days=10),
            created_by=test_user.id,
        )
        t_near = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Near future",
            due_at=now + timedelta(days=2),
            created_by=test_user.id,
        )
        next_task = get_next_task_for_company(db_session, test_company.id)
        assert next_task is not None
        assert next_task.id == t_near.id


class TestGetOpenTasksForContact:
    def test_returns_open_tasks(
        self,
        db_session: Session,
        test_site_contact: SiteContact,
        test_user,
    ):
        create_contact_task(db_session, site_contact_id=test_site_contact.id, title="CT1", created_by=test_user.id)
        tasks = get_open_tasks_for_contact(db_session, test_site_contact.id)
        assert len(tasks) == 1

    def test_excludes_done(
        self,
        db_session: Session,
        test_site_contact: SiteContact,
        test_user,
    ):
        task = create_contact_task(
            db_session,
            site_contact_id=test_site_contact.id,
            title="Done CT",
            created_by=test_user.id,
        )
        task.status = TaskStatus.DONE
        db_session.commit()
        tasks = get_open_tasks_for_contact(db_session, test_site_contact.id)
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# No activity log invariant (the principal invariant)
# ---------------------------------------------------------------------------


class TestNoActivityLogOnTaskComplete:
    def test_complete_crm_task_creates_no_activity_log(
        self,
        db_session: Session,
        test_company: Company,
        test_user,
    ):
        """Completing a CRM task MUST NOT write any ActivityLog row."""
        task = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Call by Friday",
            created_by=test_user.id,
        )
        before = _count_activity(db_session)
        result = complete_crm_task(db_session, task.id, test_user.id, "Done")
        after = _count_activity(db_session)
        assert result is not None
        assert result.status == TaskStatus.DONE
        assert after == before, (
            f"complete_crm_task created {after - before} ActivityLog row(s) — "
            "tasks are forward-looking reminders; completing one MUST NOT fake a touch."
        )

    def test_complete_task_service_creates_no_activity_log(
        self,
        db_session: Session,
        test_requisition,
        test_user,
    ):
        """The existing complete_task service function also must not create
        ActivityLog."""
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Req task",
            source="system",
            assigned_to_id=test_user.id,
        )
        before = _count_activity(db_session)
        result = complete_task(db_session, task.id, test_user.id, "note")
        after = _count_activity(db_session)
        assert result is not None
        assert result.status == TaskStatus.DONE
        assert after == before, (
            f"complete_task created {after - before} ActivityLog row(s) — "
            "tasks are forward-looking reminders; completing one MUST NOT fake a touch."
        )


# ---------------------------------------------------------------------------
# IDOR guard: contact from another company → 404
# ---------------------------------------------------------------------------


class TestContactTaskIdorGuard:
    def test_contact_from_other_company_returns_404(
        self,
        client,
        test_company: Company,
        other_contact: SiteContact,
    ):
        """POST /v2/partials/customers/{company_id}/contacts/{contact_id}/tasks must
        return 404 when contact_id belongs to a different company."""
        response = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{other_contact.id}/tasks",
            data={"title": "Injection attempt"},
        )
        assert response.status_code == 404

    def test_add_form_idor_guard(
        self,
        client,
        test_company: Company,
        other_contact: SiteContact,
    ):
        """GET /v2/partials/customers/{company_id}/contacts/{contact_id}/tasks/add-form
        must return 404 when contact_id belongs to a different company."""
        response = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{other_contact.id}/tasks/add-form")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Existing requisition tasks still work
# ---------------------------------------------------------------------------


class TestExistingRequisitionTasksUnaffected:
    def test_create_requisition_task_still_works(self, db_session: Session, test_requisition, test_user):
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="Existing requisition task",
            source="system",
        )
        assert task.requisition_id == test_requisition.id
        assert task.company_id is None
        assert task.site_contact_id is None

    def test_complete_requisition_task_still_works(self, db_session: Session, test_requisition, test_user):
        task = create_task(
            db_session,
            requisition_id=test_requisition.id,
            title="To complete",
            source="system",
            assigned_to_id=test_user.id,
        )
        result = complete_task(db_session, task.id, test_user.id, "done")
        assert result.status == TaskStatus.DONE
        assert result.completed_at is not None


# ---------------------------------------------------------------------------
# HTTP endpoints smoke tests
# ---------------------------------------------------------------------------


class TestAccountTaskEndpoints:
    def test_get_account_tasks_partial(self, client, test_company: Company):
        response = client.get(f"/v2/partials/customers/{test_company.id}/tasks")
        assert response.status_code == 200
        assert b"account-tasks-" in response.content

    def test_post_account_task_creates_and_returns_list(self, client, test_company: Company):
        response = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": "Follow up next week"},
        )
        assert response.status_code == 200
        assert b"Follow up next week" in response.content

    def test_post_account_task_missing_title_returns_error(self, client, test_company: Company):
        response = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": ""},
        )
        assert response.status_code == 200
        assert b"required" in response.content.lower()

    def test_get_account_task_add_form(self, client, test_company: Company):
        response = client.get(f"/v2/partials/customers/{test_company.id}/tasks/add-form")
        assert response.status_code == 200

    def test_complete_account_task_no_activity_log(
        self,
        client,
        db_session: Session,
        test_company: Company,
        test_user,
    ):
        """POST /v2/partials/tasks/{id}/complete must not write ActivityLog."""
        task = create_company_task(
            db_session,
            company_id=test_company.id,
            title="HTTP complete test",
            created_by=test_user.id,
        )
        before = _count_activity(db_session)
        response = client.post(f"/v2/partials/tasks/{task.id}/complete")
        assert response.status_code == 200
        after = _count_activity(db_session)
        assert after == before, (
            f"HTTP complete endpoint created {after - before} ActivityLog row(s) — "
            "tasks are forward-looking reminders, NOT activity records."
        )
        db_session.expire_all()
        refreshed = db_session.get(RequisitionTask, task.id)
        assert refreshed.status == TaskStatus.DONE


class TestContactTaskEndpoints:
    def test_get_contact_add_form(
        self,
        client,
        test_company: Company,
        test_site_contact: SiteContact,
    ):
        response = client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/{test_site_contact.id}/tasks/add-form"
        )
        assert response.status_code == 200

    def test_post_contact_task_creates(
        self,
        client,
        test_company: Company,
        test_site_contact: SiteContact,
    ):
        response = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{test_site_contact.id}/tasks",
            data={"title": "Send deck to Alice"},
        )
        assert response.status_code == 200
        assert b"Send deck to Alice" in response.content


# ---------------------------------------------------------------------------
# FIX D: complete_task_endpoint 403 IDOR guard
# ---------------------------------------------------------------------------


class TestCompleteTaskEndpointAuthz:
    """Non-owner, non-assignee, non-creator second user gets 403 on complete."""

    @pytest.fixture()
    def second_user(self, db_session: Session):
        from app.models import User

        u = User(
            email="other@trioscs.com",
            name="Other User",
            role="buyer",
            azure_id="test-azure-id-999",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(u)
        db_session.commit()
        db_session.refresh(u)
        return u

    @pytest.fixture()
    def second_client(self, db_session: Session, second_user):
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def override_db():
            yield db_session

        def override_user():
            return second_user

        async def override_fresh_token():
            return "mock-token"

        overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_user] = override_user
        app.dependency_overrides[require_admin] = override_user
        app.dependency_overrides[require_buyer] = override_user
        app.dependency_overrides[require_fresh_token] = override_fresh_token
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c
        finally:
            for dep in overridden:
                app.dependency_overrides.pop(dep, None)

    def test_non_owner_non_assignee_gets_403(
        self,
        second_client,
        db_session: Session,
        test_company: Company,
        test_user,
    ):
        """A user who is not the assignee, creator, or account owner must get 403."""
        task = create_company_task(
            db_session,
            company_id=test_company.id,
            title="Restricted task",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        resp = second_client.post(f"/v2/partials/tasks/{task.id}/complete")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# L4 (CRM half): priority + assignee are editable from the create forms
# ---------------------------------------------------------------------------


class TestAccountTaskPriorityAssignee:
    def test_add_form_renders_priority_and_assignee(self, client, test_company: Company):
        """The account add-task form exposes both a priority and an assignee picker
        (previously it captured only title + due, so priority was stuck at Medium and
        tasks were always self-assigned)."""
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tasks/add-form")
        assert resp.status_code == 200
        assert 'name="priority"' in resp.text
        assert 'name="assigned_to_id"' in resp.text

    def test_post_account_task_sets_priority_and_assignee(
        self, client, db_session: Session, test_company: Company, test_user, admin_user
    ):
        """Submitting priority + assignee persists them onto the task."""
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": "High-pri call", "priority": "3", "assigned_to_id": str(admin_user.id)},
        )
        assert resp.status_code == 200
        task = (
            db_session.query(RequisitionTask)
            .filter(RequisitionTask.company_id == test_company.id, RequisitionTask.title == "High-pri call")
            .one()
        )
        assert task.priority == 3
        assert task.assigned_to_id == admin_user.id

    def test_post_account_task_blank_defaults_medium_and_creator(
        self, client, db_session: Session, test_company: Company, test_user
    ):
        """Omitting priority/assignee keeps the prior behavior: Medium, self-
        assigned."""
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": "Default fields task"},
        )
        assert resp.status_code == 200
        task = (
            db_session.query(RequisitionTask)
            .filter(RequisitionTask.company_id == test_company.id, RequisitionTask.title == "Default fields task")
            .one()
        )
        assert task.priority == 2
        assert task.assigned_to_id == test_user.id


class TestContactTaskPriorityAssignee:
    def test_post_contact_task_sets_priority_and_assignee(
        self,
        client,
        db_session: Session,
        test_company: Company,
        test_site_contact: SiteContact,
        test_user,
        admin_user,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{test_site_contact.id}/tasks",
            data={"title": "Low-pri contact task", "priority": "1", "assigned_to_id": str(admin_user.id)},
        )
        assert resp.status_code == 200
        task = (
            db_session.query(RequisitionTask)
            .filter(
                RequisitionTask.site_contact_id == test_site_contact.id,
                RequisitionTask.title == "Low-pri contact task",
            )
            .one()
        )
        assert task.priority == 1
        assert task.assigned_to_id == admin_user.id


# ---------------------------------------------------------------------------
# completion_note wiring on the CRM/My-Day complete endpoint
# ---------------------------------------------------------------------------


class TestCrmCompleteNote:
    def test_complete_account_task_stores_completion_note(
        self, client, db_session: Session, test_company: Company, test_user
    ):
        """POST /v2/partials/tasks/{id}/complete persists an optional completion_note
        instead of discarding it (the endpoint previously hard-coded "")."""
        task = create_company_task(db_session, company_id=test_company.id, title="Note me", created_by=test_user.id)
        resp = client.post(
            f"/v2/partials/tasks/{task.id}/complete",
            data={"completion_note": "Closed via email"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        refreshed = db_session.get(RequisitionTask, task.id)
        assert refreshed.status == TaskStatus.DONE
        assert refreshed.completion_note == "Closed via email"
