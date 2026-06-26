"""Tests for CRM P0 gap wiring: task delete, task edit, account add-note.

Covers:
- Gap 1 — Task DELETE: route exists, authz gate (owner/assignee/creator/admin allow; unrelated rep → 403;
  missing task → 404); task removed from DB; refreshed list returned.
- Gap 2 — Task EDIT: GET edit-form (200); POST edit updates title/due/assignee and returns refreshed
  list; unrelated rep → 403; bad due_at → 400 (inline error fragment); task not found → 404.
- Gap 3 — Account Add Note: GET note form (200); POST creates ActivityLog NOTE row with company_id;
  response re-renders activity tab; unrelated rep → 403; note does NOT change Company.last_outbound_at.

Called by: pytest
Depends on: conftest.py (db_session, test_user, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, User
from app.models.task import RequisitionTask
from app.services.task_service import create_company_task

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """Company owned by test_user."""
    co = Company(
        name="Owned Corp",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def unrelated_user(db_session: Session) -> User:
    """A user with no relation to owned_company."""
    u = User(
        email="nobody@trioscs.com",
        name="Nobody",
        role="buyer",
        azure_id="test-azure-nobody",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def unrelated_client(db_session: Session, unrelated_user: User):
    """TestClient authenticated as unrelated_user."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return unrelated_user

    async def _token():
        return "mock-token"

    overrides = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _token
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def company_task(db_session: Session, owned_company: Company, test_user: User) -> RequisitionTask:
    """An open account task created by test_user."""
    return create_company_task(
        db_session,
        company_id=owned_company.id,
        title="Call Owned Corp",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
    )


# ---------------------------------------------------------------------------
# Gap 1 — Task DELETE
# ---------------------------------------------------------------------------


class TestTaskDeleteRoute:
    def test_owner_can_delete_task(
        self,
        client,
        db_session: Session,
        owned_company: Company,
        company_task: RequisitionTask,
    ):
        """Owner (assignee+creator) deletes task → 200, task gone from DB."""
        resp = client.delete(f"/v2/partials/tasks/{company_task.id}")
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(RequisitionTask, company_task.id) is None

    def test_delete_returns_refreshed_list(
        self,
        client,
        owned_company: Company,
        company_task: RequisitionTask,
    ):
        """Response HTML is the refreshed account-tasks partial container."""
        resp = client.delete(f"/v2/partials/tasks/{company_task.id}")
        assert resp.status_code == 200
        assert b"account-tasks-" in resp.content

    def test_unrelated_user_gets_403(
        self,
        unrelated_client,
        company_task: RequisitionTask,
    ):
        """Unrelated rep cannot delete another user's task."""
        resp = unrelated_client.delete(f"/v2/partials/tasks/{company_task.id}")
        assert resp.status_code == 403

    def test_nonexistent_task_returns_404(self, client):
        """DELETE on a missing task_id returns 404."""
        resp = client.delete("/v2/partials/tasks/999999")
        assert resp.status_code == 404

    def test_admin_can_delete_any_task(
        self,
        db_session: Session,
        owned_company: Company,
        unrelated_user: User,
        test_user: User,
    ):
        """Admin (role=admin) can delete tasks they don't own."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        admin = User(
            email="admin@trioscs.com",
            name="Admin",
            role="admin",
            azure_id="test-azure-admin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.commit()
        db_session.refresh(admin)

        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Admin delete test",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )

        def _db():
            yield db_session

        def _user():
            return admin

        async def _token():
            return "mock-token"

        overrides = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_user] = _user
        app.dependency_overrides[require_admin] = _user
        app.dependency_overrides[require_buyer] = _user
        app.dependency_overrides[require_fresh_token] = _token
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.delete(f"/v2/partials/tasks/{task.id}")
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)

        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(RequisitionTask, task.id) is None


# ---------------------------------------------------------------------------
# Gap 2 — Task EDIT
# ---------------------------------------------------------------------------


class TestTaskEditRoute:
    def test_get_edit_form_returns_200(
        self,
        client,
        company_task: RequisitionTask,
    ):
        """GET edit-form returns 200 with form HTML."""
        resp = client.get(f"/v2/partials/tasks/{company_task.id}/edit-form")
        assert resp.status_code == 200
        assert b"<form" in resp.content

    def test_get_edit_form_prefills_title(
        self,
        client,
        company_task: RequisitionTask,
    ):
        """Edit form contains the current task title."""
        resp = client.get(f"/v2/partials/tasks/{company_task.id}/edit-form")
        assert resp.status_code == 200
        assert company_task.title.encode() in resp.content

    def test_get_edit_form_nonexistent_returns_404(self, client):
        resp = client.get("/v2/partials/tasks/999999/edit-form")
        assert resp.status_code == 404

    def test_unrelated_user_cannot_get_edit_form(
        self,
        unrelated_client,
        company_task: RequisitionTask,
    ):
        resp = unrelated_client.get(f"/v2/partials/tasks/{company_task.id}/edit-form")
        assert resp.status_code == 403

    def test_post_edit_updates_title(
        self,
        client,
        db_session: Session,
        company_task: RequisitionTask,
    ):
        """POST edit with new title persists the update."""
        resp = client.post(
            f"/v2/partials/tasks/{company_task.id}/edit",
            data={"title": "Updated title"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(RequisitionTask, company_task.id)
        assert updated.title == "Updated title"

    def test_post_edit_returns_refreshed_list(
        self,
        client,
        owned_company: Company,
        company_task: RequisitionTask,
    ):
        """POST edit response is the refreshed task-list partial."""
        resp = client.post(
            f"/v2/partials/tasks/{company_task.id}/edit",
            data={"title": "New title"},
        )
        assert resp.status_code == 200
        assert b"account-tasks-" in resp.content

    def test_post_edit_bad_due_at_returns_error_fragment(
        self,
        client,
        company_task: RequisitionTask,
    ):
        """Bad due_at value returns an inline error (not 500) so HTMX can show it."""
        resp = client.post(
            f"/v2/partials/tasks/{company_task.id}/edit",
            data={"title": "Title", "due_at": "not-a-date"},
        )
        # 200 with error text (inline fragment pattern), NOT a 500
        assert resp.status_code == 200
        assert b"nvalid" in resp.content or b"date" in resp.content.lower()

    def test_unrelated_user_cannot_post_edit(
        self,
        unrelated_client,
        company_task: RequisitionTask,
    ):
        resp = unrelated_client.post(
            f"/v2/partials/tasks/{company_task.id}/edit",
            data={"title": "Hijack"},
        )
        assert resp.status_code == 403

    def test_post_edit_nonexistent_task_returns_404(self, client):
        resp = client.post(
            "/v2/partials/tasks/999999/edit",
            data={"title": "X"},
        )
        assert resp.status_code == 404

    def test_post_edit_updates_due_at(
        self,
        client,
        db_session: Session,
        company_task: RequisitionTask,
    ):
        """Valid due_at is parsed and persisted."""
        resp = client.post(
            f"/v2/partials/tasks/{company_task.id}/edit",
            data={"title": "Check due", "due_at": "2027-01-15"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(RequisitionTask, company_task.id)
        assert updated.due_at is not None
        assert updated.due_at.year == 2027
        assert updated.due_at.month == 1
        assert updated.due_at.day == 15


# ---------------------------------------------------------------------------
# Gap 3 — Account Add Note
# ---------------------------------------------------------------------------


class TestAccountAddNoteRoute:
    def test_get_add_note_form_returns_200(
        self,
        client,
        owned_company: Company,
    ):
        """GET add-note form returns 200 with textarea."""
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/activity/add-note-form")
        assert resp.status_code == 200
        assert b"textarea" in resp.content or b"note" in resp.content.lower()

    def test_post_add_note_creates_activity_log(
        self,
        client,
        db_session: Session,
        owned_company: Company,
    ):
        """POST note creates an ActivityLog NOTE row with the right company_id."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": "Test note content"},
        )
        assert resp.status_code == 200
        from app.constants import ActivityType

        note = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.company_id == owned_company.id,
                ActivityLog.activity_type == ActivityType.NOTE,
            )
            .order_by(ActivityLog.id.desc())
            .first()
        )
        assert note is not None
        assert "Test note content" in (note.notes or "")

    def test_post_add_note_does_not_bump_last_outbound_at(
        self,
        client,
        db_session: Session,
        owned_company: Company,
    ):
        """Adding a note MUST NOT change Company.last_outbound_at (notes are cadence-
        neutral)."""
        db_session.refresh(owned_company)
        before = owned_company.last_outbound_at

        client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": "Cadence-neutral note"},
        )

        db_session.expire_all()
        after = db_session.get(Company, owned_company.id).last_outbound_at
        assert after == before, (
            f"last_outbound_at changed from {before} to {after} — "
            "a note is NOT an outbound touch and must never advance the outbound clock."
        )

    def test_unrelated_user_gets_403(
        self,
        unrelated_client,
        owned_company: Company,
    ):
        """A user with no relation to the company cannot post a note."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": "Unauthorized note"},
        )
        assert resp.status_code == 403

    def test_post_add_note_nonexistent_company_returns_404(self, client):
        resp = client.post(
            "/v2/partials/customers/999999/activity/add-note",
            data={"notes": "Ghost note"},
        )
        assert resp.status_code == 404

    def test_post_add_note_empty_text_returns_error(
        self,
        client,
        owned_company: Company,
    ):
        """Empty note body returns inline error fragment (200, not 500)."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": ""},
        )
        assert resp.status_code == 200
        assert b"required" in resp.content.lower() or b"empty" in resp.content.lower()

    def test_post_add_note_re_renders_activity_tab(
        self,
        client,
        owned_company: Company,
    ):
        """Successful note POST re-renders the activity tab partial."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": "Re-render check"},
        )
        assert resp.status_code == 200
        # The activity tab template has a distinctive 'space-y-4' container
        assert b"space-y-4" in resp.content or b"activity" in resp.content.lower()


# ---------------------------------------------------------------------------
# Fix 1 — due_at clearable: empty due_at field clears stored due_at
# ---------------------------------------------------------------------------


class TestDueAtClearable:
    def test_empty_due_at_clears_existing_due_at(
        self,
        client,
        db_session: Session,
        owned_company: Company,
        test_user: User,
    ):
        """Posting the edit form with an empty due_at field must clear a previously set
        due_at."""
        from datetime import timezone

        from app.services.task_service import create_company_task

        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Has a due date",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
            due_at=datetime(2030, 6, 1, tzinfo=timezone.utc),
        )
        assert task.due_at is not None

        resp = client.post(
            f"/v2/partials/tasks/{task.id}/edit",
            data={"title": "Has a due date", "due_at": ""},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        updated = db_session.get(RequisitionTask, task.id)
        assert updated.due_at is None, "Empty due_at field must clear the stored due_at"


# ---------------------------------------------------------------------------
# Fix 2 — Contact-task cancel route and edit-form company_id correctness
# ---------------------------------------------------------------------------


@pytest.fixture()
def contact_with_task(db_session: Session, owned_company: Company, test_user: User):
    """A SiteContact under owned_company with one open task."""
    from app.models.crm import CustomerSite, SiteContact
    from app.services.task_service import create_contact_task

    site = CustomerSite(company_id=owned_company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
    )
    db_session.add(contact)
    db_session.flush()

    task = create_contact_task(
        db_session,
        site_contact_id=contact.id,
        title="Contact task",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
    )
    db_session.commit()
    db_session.refresh(site)
    db_session.refresh(contact)
    db_session.refresh(task)
    return {"site": site, "contact": contact, "task": task}


class TestContactTaskCancelRoute:
    def test_contact_tasks_refresh_route_exists(
        self,
        client,
        owned_company: Company,
        contact_with_task,
    ):
        """GET /v2/partials/customers/{company_id}/contacts/{contact_id}/tasks → 200."""
        contact = contact_with_task["contact"]
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/contacts/{contact.id}/tasks")
        assert resp.status_code == 200

    def test_contact_tasks_refresh_returns_contact_task_list(
        self,
        client,
        owned_company: Company,
        contact_with_task,
    ):
        """The contact-tasks refresh partial contains the contact-tasks container."""
        contact = contact_with_task["contact"]
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/contacts/{contact.id}/tasks")
        assert resp.status_code == 200
        assert b"contact-tasks-" in resp.content

    def test_contact_task_edit_form_has_real_company_id(
        self,
        client,
        owned_company: Company,
        contact_with_task,
    ):
        """The edit-form for a contact-scoped task must embed the real company_id, not
        0."""
        task = contact_with_task["task"]
        resp = client.get(f"/v2/partials/tasks/{task.id}/edit-form")
        assert resp.status_code == 200
        # The cancel button URL must NOT contain /customers/0/
        assert b"/customers/0/" not in resp.content
        # Must contain the real company_id
        assert str(owned_company.id).encode() in resp.content


# ---------------------------------------------------------------------------
# Fix 3 — CRM-task scope guard: requisition-only task → 400
# ---------------------------------------------------------------------------


@pytest.fixture()
def requisition_task(db_session: Session, test_user: User):
    """A task scoped only to a requisition (not a CRM task)."""
    from app.constants import TaskStatus
    from app.models.sourcing import Requisition

    req = Requisition(
        name="Test Req",
        status="open",
        created_by=test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    task = RequisitionTask(
        requisition_id=req.id,
        title="Req-scoped task",
        task_type="general",
        status=TaskStatus.TODO,
        source="manual",
        created_by=test_user.id,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


class TestCrmTaskScopeGuard:
    def test_delete_requisition_task_returns_400(
        self,
        client,
        requisition_task: RequisitionTask,
    ):
        """DELETE on a requisition-only task via the CRM route must return 400."""
        resp = client.delete(f"/v2/partials/tasks/{requisition_task.id}")
        assert resp.status_code == 400

    def test_get_edit_form_requisition_task_returns_400(
        self,
        client,
        requisition_task: RequisitionTask,
    ):
        """GET edit-form on a requisition-only task via the CRM route must return
        400."""
        resp = client.get(f"/v2/partials/tasks/{requisition_task.id}/edit-form")
        assert resp.status_code == 400

    def test_post_edit_requisition_task_returns_400(
        self,
        client,
        requisition_task: RequisitionTask,
    ):
        """POST edit on a requisition-only task via the CRM route must return 400."""
        resp = client.post(
            f"/v2/partials/tasks/{requisition_task.id}/edit",
            data={"title": "New title"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Fix 4 — Add-note form targets explicit activity-tab id
# ---------------------------------------------------------------------------


class TestAddNoteExplicitTarget:
    def test_add_note_form_has_explicit_target_id(
        self,
        client,
        owned_company: Company,
    ):
        """The add-note form must use an explicit hx-target id, not 'closest
        .space-y-4'."""
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/activity/add-note-form")
        assert resp.status_code == 200
        assert b"closest .space-y-4" not in resp.content

    def test_post_add_note_swaps_into_activity_tab_container(
        self,
        client,
        owned_company: Company,
    ):
        """POST add-note response must contain the activity-tab container id."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/activity/add-note",
            data={"notes": "Explicit target check"},
        )
        assert resp.status_code == 200
        # The activity tab container id should be present in the re-rendered content
        expected_id = f"activity-tab-{owned_company.id}".encode()
        assert expected_id in resp.content
