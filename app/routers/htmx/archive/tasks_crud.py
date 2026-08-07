"""Task list / add-form / create partials for accounts, contacts, and vendors.

W4.8 split of the 969-line app/routers/htmx/archive.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__). The vendor-task routes (originally
after the task-lifecycle block) now register before it — safe: /v2/partials/vendors/...
and /v2/partials/tasks/... share no matchable prefix.

Called by: app/main.py (router mount via the package __init__).
Depends on: app.database, app.dependencies, app.models, app.services.task_service,
    app.template_env, ..._lookup_helpers, .._shared, .common
"""

from datetime import UTC, date, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import (
    can_manage_account,
    require_user,
)
from ....models import (
    Company,
    CustomerSite,
    SiteContact,
    User,
)
from ....template_env import template_response
from ..._lookup_helpers import get_vendor_card_or_404
from .._shared import _base_ctx, _safe_int
from .common import _active_users, _coerce_task_priority, router

# ── Step 5: Account/Contact Tasks ────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/tasks", response_class=HTMLResponse)
async def account_tasks_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for an account."""
    from app.services.task_service import get_open_tasks_for_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    tasks = get_open_tasks_for_company(db, company_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["company_tasks"] = tasks
    return template_response("htmx/partials/customers/_account_tasks.html", ctx)


@router.get("/v2/partials/customers/{company_id}/tasks/add-form", response_class=HTMLResponse)
async def account_task_add_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for an account."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["users"] = _active_users(db)
    ctx["current_user_id"] = user.id
    return template_response("htmx/partials/customers/_account_task_form.html", ctx)


@router.post("/v2/partials/customers/{company_id}/tasks", response_class=HTMLResponse)
async def create_account_task(
    request: Request,
    company_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    priority: str = Form("2"),
    assigned_to_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to an account; return refreshed task list."""

    from app.services.task_service import create_company_task, get_open_tasks_for_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the account owner or an admin can create tasks for this account")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_company_task(
        db,
        company_id=company_id,
        title=title.strip(),
        due_at=due_dt,
        priority=_coerce_task_priority(priority),
        created_by=user.id,
        assigned_to_id=_safe_int(assigned_to_id) or user.id,
    )
    tasks = get_open_tasks_for_company(db, company_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["company_tasks"] = tasks
    return template_response("htmx/partials/customers/_account_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks/add-form",
    response_class=HTMLResponse,
)
async def contact_task_add_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for a contact."""
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["contact_id"] = contact_id
    ctx["users"] = _active_users(db)
    ctx["current_user_id"] = user.id
    return template_response("htmx/partials/customers/_contact_task_form.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks",
    response_class=HTMLResponse,
)
async def create_contact_task_endpoint(
    request: Request,
    company_id: int,
    contact_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    priority: str = Form("2"),
    assigned_to_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a contact; return refreshed contact task list."""

    from app.services.task_service import create_contact_task, get_open_tasks_for_contact

    # Scoped-join IDOR guard: contact must belong to this company
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company:
        if not can_manage_account(user, company, db):
            raise HTTPException(403, "Only the account owner or an admin can create tasks for this account")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_contact_task(
        db,
        site_contact_id=contact_id,
        title=title.strip(),
        due_at=due_dt,
        priority=_coerce_task_priority(priority),
        created_by=user.id,
        assigned_to_id=_safe_int(assigned_to_id) or user.id,
    )
    tasks = get_open_tasks_for_contact(db, contact_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["contact"] = contact
    ctx["contact_tasks"] = tasks
    ctx["company_id"] = company_id
    ctx["site_id"] = contact.customer_site_id
    return template_response("htmx/partials/customers/_contact_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks",
    response_class=HTMLResponse,
)
async def contact_tasks_partial(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for a contact (used as cancel target in edit form)."""
    from app.services.task_service import get_open_tasks_for_contact

    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    tasks = get_open_tasks_for_contact(db, contact_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["contact"] = contact
    ctx["contact_tasks"] = tasks
    ctx["company_id"] = company_id
    ctx["site_id"] = contact.customer_site_id
    return template_response("htmx/partials/customers/_contact_tasks.html", ctx)


# ---------------------------------------------------------------------------
# Vendor task routes
# ---------------------------------------------------------------------------


@router.get("/v2/partials/vendors/{vendor_id}/tasks", response_class=HTMLResponse)
async def vendor_tasks_partial(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for a vendor card."""
    from app.services.task_service import get_open_tasks_for_vendor_card

    vendor = get_vendor_card_or_404(db, vendor_id)
    tasks = get_open_tasks_for_vendor_card(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["vendor"] = vendor
    ctx["vendor_tasks"] = tasks
    return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tasks/add-form", response_class=HTMLResponse)
async def vendor_task_add_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for a vendor card."""
    get_vendor_card_or_404(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["users"] = _active_users(db)
    ctx["current_user_id"] = user.id
    return template_response("htmx/partials/vendors/tabs/_vendor_task_form.html", ctx)


@router.post("/v2/partials/vendors/{vendor_id}/tasks", response_class=HTMLResponse)
async def create_vendor_task_endpoint(
    request: Request,
    vendor_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    priority: str = Form("2"),
    assigned_to_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a vendor; return refreshed task list."""
    from datetime import date as _date

    from app.services.task_service import create_vendor_task, get_open_tasks_for_vendor_card

    vendor = get_vendor_card_or_404(db, vendor_id)
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = _date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_vendor_task(
        db,
        vendor_card_id=vendor_id,
        title=title.strip(),
        due_at=due_dt,
        priority=_coerce_task_priority(priority),
        created_by=user.id,
        assigned_to_id=_safe_int(assigned_to_id) or user.id,
    )
    tasks = get_open_tasks_for_vendor_card(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["vendor"] = vendor
    ctx["vendor_tasks"] = tasks
    return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
