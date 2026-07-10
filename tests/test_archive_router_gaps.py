"""test_archive_router_gaps.py — Coverage gap tests for app/routers/htmx/archive.py.

Targets uncovered lines: 210, 217, 291, 293, 296-300, 339, 391-398, 419-421,
470-477, 498-499, 526-528, 576, 604-632, 659, 676-704, 739-742, 765-769, 799,
801, 865-868.

Called by: pytest
Depends on: conftest fixtures, app.models, app.services.task_service
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import Company, CustomerSite, RequisitionTask, SiteContact, User, VendorCard
from app.models.vendors import VendorContact
from tests.conftest import engine, sqlite_fk_disabled  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html_ok(_name: str, _ctx: dict, **_kw):
    """Stub for template_response — returns minimal HTMLResponse."""
    return HTMLResponse("<html/>")


def _make_company(db: Session, owner_id: int | None = None) -> Company:
    co = Company(
        name="Test Co Gap",
        website="https://testcogap.com",
        is_active=True,
        created_at=datetime.now(UTC),
        account_owner_id=owner_id,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company_id: int, owner_id: int | None = None) -> CustomerSite:
    site = CustomerSite(
        company_id=company_id,
        site_name="Main Site",
        owner_id=owner_id,
    )
    db.add(site)
    db.flush()
    return site


def _make_contact(db: Session, site_id: int) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site_id,
        full_name="Jane Gap",
        email="jane@gap.com",
        is_active=True,
    )
    db.add(contact)
    db.flush()
    return contact


def _make_vendor_card(db: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="gap vendor",
        display_name="Gap Vendor",
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_vendor_contact(db: Session, vendor_card_id: int) -> VendorContact:
    vc = VendorContact(
        vendor_card_id=vendor_card_id,
        full_name="Vendor Guy",
        email="vendor@gap.com",
        source="manual",
    )
    db.add(vc)
    db.flush()
    return vc


def _make_task(
    db: Session,
    user_id: int,
    company_id: int | None = None,
    site_contact_id: int | None = None,
    vendor_card_id: int | None = None,
    vendor_contact_id: int | None = None,
    title: str = "Test Task",
) -> RequisitionTask:
    task = RequisitionTask(
        company_id=company_id,
        site_contact_id=site_contact_id,
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        title=title,
        task_type="general",
        status=TaskStatus.TODO,
        created_by=user_id,
        assigned_to_id=user_id,
        created_at=datetime.now(UTC),
    )
    db.add(task)
    db.flush()
    return task


# ---------------------------------------------------------------------------
# Admin client fixture — required for admin-only routes
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user(db_session: Session) -> User:
    user = User(
        email="admin_gap@trioscs.com",
        name="Admin Gap",
        role="admin",
        azure_id="admin-gap-azure-001",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user
    app.dependency_overrides[require_fresh_token] = lambda: "token"
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Line 210 — create_account_task: 403 when user is not account owner/admin
# ---------------------------------------------------------------------------


def test_create_account_task_403_not_owner(client, db_session, test_user):
    """POST /v2/partials/customers/{id}/tasks returns 403 when user lacks ownership."""
    # Company with a different owner — test_user is not admin so can't manage
    co = _make_company(db_session, owner_id=None)
    db_session.commit()
    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok):
        resp = client.post(
            f"/v2/partials/customers/{co.id}/tasks",
            data={"title": "Task X", "due_at": ""},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Line 217 — create_account_task: invalid date returns 422 HTML error
# ---------------------------------------------------------------------------


def test_create_account_task_invalid_date(client, db_session, test_user):
    """POST /v2/partials/customers/{id}/tasks returns date error on bad due_at."""
    co = _make_company(db_session, owner_id=test_user.id)
    db_session.commit()
    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok):
        resp = client.post(
            f"/v2/partials/customers/{co.id}/tasks",
            data={"title": "Good Title", "due_at": "not-a-date"},
        )
    assert resp.status_code == 200
    assert "Invalid date" in resp.text


# ---------------------------------------------------------------------------
# Line 291, 293, 296-300 — create_contact_task: 403 and invalid date
# ---------------------------------------------------------------------------


def test_create_contact_task_403_not_owner(client, db_session, test_user):
    """POST contact tasks returns 403 when user can't manage the account."""
    co = _make_company(db_session, owner_id=None)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    db_session.commit()
    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok):
        resp = client.post(
            f"/v2/partials/customers/{co.id}/contacts/{contact.id}/tasks",
            data={"title": "Contact Task", "due_at": ""},
        )
    assert resp.status_code == 403


def test_create_contact_task_invalid_date(client, db_session, test_user):
    """POST contact tasks returns date error on bad due_at."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    db_session.commit()
    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok):
        resp = client.post(
            f"/v2/partials/customers/{co.id}/contacts/{contact.id}/tasks",
            data={"title": "Real Title", "due_at": "bad-date"},
        )
    assert resp.status_code == 200
    assert "Invalid date" in resp.text


# ---------------------------------------------------------------------------
# Line 339 — contact_tasks_partial: contact not found
# ---------------------------------------------------------------------------


def test_contact_tasks_partial_404(client, db_session):
    """GET contact tasks returns 404 if contact doesn't exist."""
    co = _make_company(db_session)
    db_session.commit()
    resp = client.get(f"/v2/partials/customers/{co.id}/contacts/99999/tasks")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Lines 391-398 — complete_task: vendor_contact_id path (vc found + deleted)
# ---------------------------------------------------------------------------


def test_complete_task_vendor_contact_found(client, db_session, test_user):
    """POST complete on a vendor_contact task returns vendor tasks HTML."""
    card = _make_vendor_card(db_session)
    vc = _make_vendor_contact(db_session, card.id)
    task = _make_task(db_session, test_user.id, vendor_contact_id=vc.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(f"/v2/partials/tasks/{task.id}/complete")
    assert resp.status_code == 200
    # Must resolve vendor_id via the vendor_contact -> vendor_card_id lookup and
    # render the vendor tasks partial (not the account/contact one).
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id


def test_complete_task_vendor_contact_deleted(client, db_session, test_user):
    """POST complete on a vendor_contact task when VendorContact row is gone."""
    # Disable FK checks to insert a task with dangling vendor_contact_id.
    with sqlite_fk_disabled(db_session):
        task = RequisitionTask(
            vendor_contact_id=99999,
            title="Orphan VC Task",
            task_type="general",
            status=TaskStatus.TODO,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(task)
        db_session.flush()

    resp = client.post(f"/v2/partials/tasks/{task.id}/complete")
    assert resp.status_code == 200
    assert "Task updated" in resp.text


# ---------------------------------------------------------------------------
# Lines 419-421 — complete_task: from_my_day=true and fallback empty fragment
# ---------------------------------------------------------------------------


def test_complete_task_from_my_day(client, db_session, test_user):
    """POST complete with from_my_day=true returns empty fragment."""
    co = _make_company(db_session, owner_id=test_user.id)
    task = _make_task(db_session, test_user.id, company_id=co.id)
    db_session.commit()

    resp = client.post(f"/v2/partials/tasks/{task.id}/complete?from_my_day=true")
    assert resp.status_code == 200
    assert resp.text == ""


def test_complete_task_fallback_empty_fragment(client, db_session, test_user):
    """POST complete on a requisition-only task returns empty fragment."""
    # Task with only requisition_id set — no company/contact/vendor
    from app.models import Requisition

    req = Requisition(
        name="REQ-FALLBACK",
        customer_name="Fallback Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()
    task = RequisitionTask(
        requisition_id=req.id,
        title="Req Only Task",
        task_type="general",
        status=TaskStatus.TODO,
        created_by=test_user.id,
        assigned_to_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(task)
    db_session.commit()

    resp = client.post(f"/v2/partials/tasks/{task.id}/complete")
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------------------
# Lines 470-477 — delete_task: vendor_card and vendor_contact branches
# ---------------------------------------------------------------------------


def test_delete_task_vendor_card_branch(admin_client, db_session, admin_user):
    """DELETE vendor_card task by admin returns vendor task list HTML."""
    card = _make_vendor_card(db_session)
    task = _make_task(db_session, admin_user.id, vendor_card_id=card.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = admin_client.delete(f"/v2/partials/tasks/{task.id}")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id
    # Task must actually be gone, not just re-rendered.
    assert db_session.get(RequisitionTask, task.id) is None


def test_delete_task_vendor_contact_branch(admin_client, db_session, admin_user):
    """DELETE vendor_contact task by admin returns vendor tasks HTML."""
    card = _make_vendor_card(db_session)
    vc = _make_vendor_contact(db_session, card.id)
    task = _make_task(db_session, admin_user.id, vendor_contact_id=vc.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = admin_client.delete(f"/v2/partials/tasks/{task.id}")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    # Resolved through the vendor_contact -> vendor_card_id lookup.
    assert ctx["vendor_id"] == card.id
    assert db_session.get(RequisitionTask, task.id) is None


# ---------------------------------------------------------------------------
# Lines 498-499 — delete_task: vendor_contact deleted fallback + empty return
# ---------------------------------------------------------------------------


def test_delete_task_vendor_contact_deleted_fallback(admin_client, db_session, admin_user):
    """DELETE task whose vendor_contact no longer exists returns safe ack."""
    with sqlite_fk_disabled(db_session):
        task = RequisitionTask(
            vendor_contact_id=99998,
            title="Orphan Delete Task",
            task_type="general",
            status=TaskStatus.TODO,
            created_by=admin_user.id,
            assigned_to_id=admin_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(task)
        db_session.flush()

    resp = admin_client.delete(f"/v2/partials/tasks/{task.id}")
    assert resp.status_code == 200
    assert "Task deleted" in resp.text


# ---------------------------------------------------------------------------
# Lines 526-528 — task_edit_form: vendor_contact path vendor_id resolution
# ---------------------------------------------------------------------------


def test_task_edit_form_vendor_contact(client, db_session, test_user):
    """GET edit-form for a vendor_contact task resolves vendor_id."""
    card = _make_vendor_card(db_session)
    vc = _make_vendor_contact(db_session, card.id)
    task = _make_task(db_session, test_user.id, vendor_contact_id=vc.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.get(f"/v2/partials/tasks/{task.id}/edit-form")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_task_edit_form.html"
    # vendor_id must be resolved via vendor_contact -> vendor_card_id, not left at 0.
    assert ctx["vendor_id"] == card.id
    assert ctx["task"].id == task.id


# ---------------------------------------------------------------------------
# Line 576 — edit_task: invalid date format
# ---------------------------------------------------------------------------


def test_edit_task_invalid_date(client, db_session, test_user):
    """POST /v2/partials/tasks/{id}/edit with bad due_at returns error HTML."""
    co = _make_company(db_session, owner_id=test_user.id)
    task = _make_task(db_session, test_user.id, company_id=co.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/tasks/{task.id}/edit",
        data={"title": "New Title", "due_at": "not-a-date"},
    )
    assert resp.status_code == 200
    assert "Invalid date" in resp.text


# ---------------------------------------------------------------------------
# Lines 604-632 — edit_task: vendor_card and vendor_contact return branches
# ---------------------------------------------------------------------------


def test_edit_task_vendor_card_branch(client, db_session, test_user):
    """POST edit returns vendor task list when task is vendor-scoped."""
    card = _make_vendor_card(db_session)
    task = _make_task(db_session, test_user.id, vendor_card_id=card.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/tasks/{task.id}/edit",
            data={"title": "Updated Vendor Task", "due_at": ""},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id
    # The edit must have actually persisted, not just re-rendered stale data.
    db_session.refresh(task)
    assert task.title == "Updated Vendor Task"


def test_edit_task_vendor_contact_branch(client, db_session, test_user):
    """POST edit returns vendor task list for vendor_contact task."""
    card = _make_vendor_card(db_session)
    vc = _make_vendor_contact(db_session, card.id)
    task = _make_task(db_session, test_user.id, vendor_contact_id=vc.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/tasks/{task.id}/edit",
            data={"title": "Updated VC Task", "due_at": ""},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id
    db_session.refresh(task)
    assert task.title == "Updated VC Task"


# ---------------------------------------------------------------------------
# Line 659 — snooze_task: 400 "Not a CRM task"
# ---------------------------------------------------------------------------


def test_snooze_task_not_crm_task(client, db_session, test_user):
    """POST snooze on a requisition-only task returns 400."""
    from app.models import Requisition

    req = Requisition(
        name="REQ-SNOOZE",
        customer_name="Snooze Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()
    task = RequisitionTask(
        requisition_id=req.id,
        title="Req Snooze Task",
        task_type="general",
        status=TaskStatus.TODO,
        created_by=test_user.id,
        assigned_to_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(task)
    db_session.commit()

    resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Lines 676-704 — snooze_task: vendor_card and vendor_contact return branches
# ---------------------------------------------------------------------------


def test_snooze_task_vendor_card_branch(client, db_session, test_user):
    """POST snooze returns vendor task list for vendor_card task."""
    card = _make_vendor_card(db_session)
    task = _make_task(db_session, test_user.id, vendor_card_id=card.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id
    # No due_at previously: snooze must set it to tomorrow, not leave it unset.
    db_session.refresh(task)
    assert task.due_at is not None


def test_snooze_task_vendor_contact_branch(client, db_session, test_user):
    """POST snooze returns vendor task list for vendor_contact task."""
    card = _make_vendor_card(db_session)
    vc = _make_vendor_contact(db_session, card.id)
    task = _make_task(db_session, test_user.id, vendor_contact_id=vc.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    assert ctx["vendor_id"] == card.id
    db_session.refresh(task)
    assert task.due_at is not None


# ---------------------------------------------------------------------------
# Lines 739-742 — create_vendor_task: invalid date
# ---------------------------------------------------------------------------


def test_create_vendor_task_invalid_date(client, db_session):
    """POST vendor tasks with bad due_at returns date error HTML."""
    card = _make_vendor_card(db_session)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok):
        resp = client.post(
            f"/v2/partials/vendors/{card.id}/tasks",
            data={"title": "Vendor Task", "due_at": "bad-date"},
        )
    assert resp.status_code == 200
    assert "Invalid date" in resp.text


# ---------------------------------------------------------------------------
# Lines 765-769 — activity_add_note_form: 403 when user can't manage account
# ---------------------------------------------------------------------------


def test_activity_add_note_form_403(client, db_session, test_user):
    """GET add-note-form returns 403 when user lacks account management access."""
    co = _make_company(db_session, owner_id=None)
    db_session.commit()

    resp = client.get(f"/v2/partials/customers/{co.id}/activity/add-note-form")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Lines 799, 801 — activity_add_note: 403 and empty note error
# ---------------------------------------------------------------------------


def test_activity_add_note_403(client, db_session, test_user):
    """POST add-note returns 403 when user can't manage account."""
    co = _make_company(db_session, owner_id=None)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/customers/{co.id}/activity/add-note",
        data={"notes": "Some note"},
    )
    assert resp.status_code == 403


def test_activity_add_note_empty_note(client, db_session, test_user):
    """POST add-note returns error HTML when notes is empty."""
    co = _make_company(db_session, owner_id=test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/customers/{co.id}/activity/add-note",
        data={"notes": "   "},
    )
    assert resp.status_code == 200
    assert "Note text is required" in resp.text


# ---------------------------------------------------------------------------
# Line 217 — create_account_task: valid date path (try block success)
# ---------------------------------------------------------------------------


def test_create_account_task_with_valid_date(client, db_session, test_user):
    """POST create_account_task with a valid ISO due_at hits the try-block success."""
    co = _make_company(db_session, owner_id=test_user.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/customers/{co.id}/tasks",
            data={"title": "Task With Date", "due_at": "2026-12-31"},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_account_tasks.html"
    # The parsed due_at must actually have persisted on a newly created task.
    created = [t for t in ctx["company_tasks"] if t.title == "Task With Date"]
    assert len(created) == 1
    assert created[0].due_at.date().isoformat() == "2026-12-31"


# ---------------------------------------------------------------------------
# Line 293, 298 — create_contact_task: empty title + valid date path
# ---------------------------------------------------------------------------


def test_create_contact_task_empty_title(client, db_session, test_user):
    """POST contact task with empty title returns HTML error (line 293)."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/customers/{co.id}/contacts/{contact.id}/tasks",
        data={"title": "   ", "due_at": ""},
    )
    assert resp.status_code == 200
    assert "Title is required" in resp.text


def test_create_contact_task_with_valid_date(client, db_session, test_user):
    """POST contact task with valid ISO due_at covers the try-block success (line
    298)."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/customers/{co.id}/contacts/{contact.id}/tasks",
            data={"title": "Contact With Date", "due_at": "2026-11-30"},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_contact_tasks.html"
    created = [t for t in ctx["contact_tasks"] if t.title == "Contact With Date"]
    assert len(created) == 1
    assert created[0].due_at.date().isoformat() == "2026-11-30"


# ---------------------------------------------------------------------------
# Lines 391-398 — complete_task: site_contact branch
# ---------------------------------------------------------------------------


def test_complete_task_site_contact_branch(client, db_session, test_user):
    """POST complete on a site_contact task returns contact task list."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    task = _make_task(db_session, test_user.id, site_contact_id=contact.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(f"/v2/partials/tasks/{task.id}/complete")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_contact_tasks.html"
    assert ctx["contact"].id == contact.id
    assert ctx["company_id"] == co.id
    # Completed task is no longer open, so it must drop out of the open list.
    assert task.id not in [t.id for t in ctx["contact_tasks"]]


# ---------------------------------------------------------------------------
# Lines 470-477 — delete_task: site_contact branch
# ---------------------------------------------------------------------------


def test_delete_task_site_contact_branch(client, db_session, test_user):
    """DELETE a site_contact task returns refreshed contact task list."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    task = _make_task(db_session, test_user.id, site_contact_id=contact.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.delete(f"/v2/partials/tasks/{task.id}")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_contact_tasks.html"
    assert ctx["contact"].id == contact.id
    assert ctx["company_id"] == co.id
    assert db_session.get(RequisitionTask, task.id) is None


# ---------------------------------------------------------------------------
# Lines 604-612 — edit_task: site_contact branch
# ---------------------------------------------------------------------------


def test_edit_task_site_contact_branch(client, db_session, test_user):
    """POST edit on a site_contact task returns contact task list HTML."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    task = _make_task(db_session, test_user.id, site_contact_id=contact.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/tasks/{task.id}/edit",
            data={"title": "Updated Contact Task", "due_at": ""},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_contact_tasks.html"
    assert ctx["contact"].id == contact.id
    assert ctx["company_id"] == co.id
    db_session.refresh(task)
    assert task.title == "Updated Contact Task"


# ---------------------------------------------------------------------------
# Line 632 — edit_task: vendor_contact deleted fallback (empty HTMLResponse)
# ---------------------------------------------------------------------------


def test_edit_task_vendor_contact_deleted_fallback(client, db_session, test_user):
    """POST edit on vendor_contact task when VC is gone returns empty fragment."""
    with sqlite_fk_disabled(db_session):
        task = RequisitionTask(
            vendor_contact_id=99997,
            title="Edit VC Orphan",
            task_type="general",
            status=TaskStatus.TODO,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(task)
        db_session.flush()

    resp = client.post(
        f"/v2/partials/tasks/{task.id}/edit",
        data={"title": "New Title", "due_at": ""},
    )
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------------------
# Lines 676-684 — snooze_task: site_contact branch
# ---------------------------------------------------------------------------


def test_snooze_task_site_contact_branch(client, db_session, test_user):
    """POST snooze on a site_contact task returns refreshed contact task list."""
    co = _make_company(db_session, owner_id=test_user.id)
    site = _make_site(db_session, co.id)
    contact = _make_contact(db_session, site.id)
    task = _make_task(db_session, test_user.id, site_contact_id=contact.id)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/customers/_contact_tasks.html"
    assert ctx["contact"].id == contact.id
    assert ctx["company_id"] == co.id
    db_session.refresh(task)
    assert task.due_at is not None


# ---------------------------------------------------------------------------
# Line 704 — snooze_task: vendor_contact deleted fallback (empty HTMLResponse)
# ---------------------------------------------------------------------------


def test_snooze_task_vendor_contact_deleted_fallback(client, db_session, test_user):
    """POST snooze on vendor_contact task when VC is gone returns empty fragment."""
    with sqlite_fk_disabled(db_session):
        task = RequisitionTask(
            vendor_contact_id=99996,
            title="Snooze VC Orphan",
            task_type="general",
            status=TaskStatus.TODO,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(task)
        db_session.flush()

    resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------------------
# Lines 865-868 — vendor_activity_add_note: empty notes returns error HTML
# ---------------------------------------------------------------------------


def test_vendor_activity_add_note_empty(client, db_session):
    """POST vendor add-note returns error HTML when notes field is blank."""
    card = _make_vendor_card(db_session)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/vendors/{card.id}/activity/add-note",
        data={"notes": ""},
    )
    assert resp.status_code == 200
    assert "Note text is required" in resp.text


# ---------------------------------------------------------------------------
# Line 576 — edit_task: empty title returns error HTML
# ---------------------------------------------------------------------------


def test_edit_task_empty_title(client, db_session, test_user):
    """POST edit_task with blank title returns 'Title is required' HTML (line 576)."""
    co = _make_company(db_session, owner_id=test_user.id)
    task = _make_task(db_session, test_user.id, company_id=co.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/tasks/{task.id}/edit",
        data={"title": "   ", "due_at": ""},
    )
    assert resp.status_code == 200
    assert "Title is required" in resp.text


# ---------------------------------------------------------------------------
# Lines 739-742 — vendor_task_add_form: GET renders form
# ---------------------------------------------------------------------------


def test_vendor_task_add_form_get(client, db_session):
    """GET /v2/partials/vendors/{id}/tasks/add-form renders the task form."""
    card = _make_vendor_card(db_session)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.get(f"/v2/partials/vendors/{card.id}/tasks/add-form")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_task_form.html"
    assert ctx["vendor_id"] == card.id


# ---------------------------------------------------------------------------
# Line 767 — create_vendor_task: valid date path (try-block success)
# ---------------------------------------------------------------------------


def test_create_vendor_task_with_valid_date(client, db_session):
    """POST vendor task with valid ISO due_at covers the try-block success (line
    767)."""
    card = _make_vendor_card(db_session)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.post(
            f"/v2/partials/vendors/{card.id}/tasks",
            data={"title": "Vendor Task With Date", "due_at": "2026-12-31"},
        )
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/tabs/_vendor_tasks.html"
    created = [t for t in ctx["vendor_tasks"] if t.title == "Vendor Task With Date"]
    assert len(created) == 1
    assert created[0].due_at.date().isoformat() == "2026-12-31"


# ---------------------------------------------------------------------------
# Line 799 — activity_add_note_form GET: company not found 404
# ---------------------------------------------------------------------------


def test_activity_add_note_form_404(client, db_session):
    """GET add-note-form for nonexistent company returns 404 (line 799)."""
    resp = client.get("/v2/partials/customers/99999/activity/add-note-form")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Lines 865-868 — vendor_activity_add_note_form: GET renders form
# ---------------------------------------------------------------------------


def test_vendor_activity_add_note_form_get(client, db_session):
    """GET /v2/partials/vendors/{id}/activity/add-note-form renders the form."""
    card = _make_vendor_card(db_session)
    db_session.commit()

    with patch("app.routers.htmx.archive.template_response", side_effect=_html_ok) as mock_render:
        resp = client.get(f"/v2/partials/vendors/{card.id}/activity/add-note-form")
    assert resp.status_code == 200
    template_name, ctx = mock_render.call_args[0]
    assert template_name == "htmx/partials/vendors/_add_note_form.html"
    assert ctx["vendor_id"] == card.id
