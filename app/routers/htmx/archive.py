"""routers/htmx/archive.py — Tasks / tickets lifecycle partials (HTMX + Alpine).

Server-rendered HTML partials for the task/ticket lifecycle surface: trouble-ticket
workspace/list/detail, account + contact + vendor tasks (add-form/create/list),
task complete/delete/edit/snooze, and the account/contact + vendor activity
add-note forms. Extracted verbatim from htmx_views.py (same `/v2/partials/...` paths,
same `htmx-views` tag).

Called by: app/main.py (router mount); htmx_views.py re-imports
    `_build_ticket_list_context` for error_reports.analyze_tickets.
Depends on: app.models, app.dependencies, app.database, app.services, ._shared,
    .companies (company_tab), .vendors (vendor_tab)
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    TicketSource,
    TicketStatus,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    can_manage_account,
    require_admin,
    require_user,
)
from ...models import (
    Company,
    CustomerSite,
    RequisitionTask,
    SiteContact,
    User,
)
from ...template_env import template_response
from .._lookup_helpers import get_vendor_card_or_404
from ._shared import _base_ctx
from .companies import company_tab
from .vendors import vendor_tab

router = APIRouter(tags=["htmx-views"])


# ── Trouble Tickets ──────────────────────────────────────────────────────


@router.get("/v2/partials/trouble-tickets/workspace", response_class=HTMLResponse)
async def trouble_tickets_workspace(request: Request, user: User = Depends(require_admin)):
    """Trouble Tickets workspace — loaded into #settings-content (admin-only
    console)."""
    return template_response(
        "htmx/partials/tickets/workspace.html",
        {**_base_ctx(request, user, "tickets")},
    )


def _build_ticket_list_context(db: Session, status: str | None) -> dict:
    """Query + group report_button tickets for the list partial.

    Shared by trouble_tickets_list and error_reports.analyze_tickets so both
    render the same grouped view. A logical ``status == "open"`` expands to the
    (submitted, in_progress) set so in-progress tickets stay visible under the
    "Open" pill; any other truthy status is an exact match; falsy means "all".

    Called by: trouble_tickets_list, error_reports.analyze_tickets.
    Depends on: TroubleTicket / RootCauseGroup models.
    """
    from app.models.root_cause_group import RootCauseGroup
    from app.models.trouble_ticket import TroubleTicket

    q = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
    )
    if status == "open":
        q = q.filter(TroubleTicket.status.in_([TicketStatus.SUBMITTED, TicketStatus.IN_PROGRESS]))
    elif status:
        q = q.filter(TroubleTicket.status == status)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(200).all()
    total = len(tickets)

    # Build group lookup only from group IDs present in results
    group_ids = {t.root_cause_group_id for t in tickets if t.root_cause_group_id}
    groups = (
        db.query(RootCauseGroup).filter(RootCauseGroup.id.in_(group_ids)).order_by(RootCauseGroup.title).all()
        if group_ids
        else []
    )
    grouped: dict = {}
    ungrouped = []
    for t in tickets:
        if t.root_cause_group_id:
            grouped.setdefault(t.root_cause_group_id, []).append(t)
        else:
            ungrouped.append(t)

    return {
        "total": total,
        "groups": groups,
        "grouped": grouped,
        "ungrouped": ungrouped,
        "current_status": status or "",
    }


@router.get("/v2/partials/trouble-tickets/list", response_class=HTMLResponse)
async def trouble_tickets_list(
    request: Request,
    status: str = "",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Tickets list partial — grouped by root cause, filterable by status."""
    return template_response(
        "htmx/partials/tickets/list.html",
        {**_base_ctx(request, user, "tickets"), **_build_ticket_list_context(db, status)},
    )


@router.get("/v2/partials/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Ticket detail partial — swapped into #main-content (admin-only
    console)."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.id == ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return template_response(
        "htmx/partials/tickets/detail.html",
        {**_base_ctx(request, user, "tickets"), "ticket": ticket},
    )


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
    return template_response("htmx/partials/customers/_account_task_form.html", ctx)


@router.post("/v2/partials/customers/{company_id}/tasks", response_class=HTMLResponse)
async def create_account_task(
    request: Request,
    company_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to an account; return refreshed task list."""
    from datetime import timezone as _tz

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
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_company_task(
        db,
        company_id=company_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
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
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a contact; return refreshed contact task list."""
    from datetime import timezone as _tz

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
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_contact_task(
        db,
        site_contact_id=contact_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
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


@router.post("/v2/partials/tasks/{task_id}/complete", response_class=HTMLResponse)
async def complete_task_endpoint(
    request: Request,
    task_id: int,
    from_my_day: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a CRM task done (CRM account/contact or vendor card/contact). No activity
    log is created.

    Permissive auth: the caller only needs require_user — any logged-in user may mark
    a vendor task done (vendor tasks carry no ownership gate at complete time).

    Returns the refreshed parent task list (account, contact, or vendor card). When
    from_my_day=true, returns an empty fragment so the row removes itself via outerHTML
    swap on the My Day worklist.
    """
    from app.services.task_service import (
        complete_crm_task,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        complete_crm_task(db, task_id, user.id, is_admin=(user.role == UserRole.ADMIN))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # My Day context: caller handles its own row removal via outerHTML swap.
    if from_my_day:
        return HTMLResponse("")
    # Re-render the appropriate parent container
    if task.company_id:
        tasks = get_open_tasks_for_company(db, task.company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = task.company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if task.site_contact_id:
        contact = db.get(SiteContact, task.site_contact_id)
        tasks = get_open_tasks_for_contact(db, task.site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = task.site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if task.vendor_card_id:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, task.vendor_card_id)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = task.vendor_card_id
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if task.vendor_contact_id:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, task.vendor_contact_id)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
        # VendorContact was deleted — return a safe non-blank acknowledgement.
        return HTMLResponse('<p class="text-xs text-gray-400">Task updated.</p>')
    # Fallback: requisition task — just return empty fragment
    return HTMLResponse("")


@router.delete("/v2/partials/tasks/{task_id}", response_class=HTMLResponse)
async def delete_task_endpoint(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a CRM task. Same authz gate as complete_task_endpoint.

    Returns the refreshed parent task list (account or contact).
    """
    from app.services.task_service import (
        delete_task,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    is_vendor_task = task.vendor_card_id is not None or task.vendor_contact_id is not None
    is_crm_task = task.company_id is not None or task.site_contact_id is not None
    if not is_crm_task and not is_vendor_task:
        raise HTTPException(400, "Not a CRM task")
    from app.services.task_service import _is_crm_task_authorized

    # Vendor task delete requires admin; customer task uses the full authz gate.
    if is_vendor_task and not is_crm_task:
        if user.role != UserRole.ADMIN:
            raise HTTPException(403, "Only admins can delete vendor tasks")
    elif not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to delete this task")
    # Capture parent refs before deletion
    company_id = task.company_id
    site_contact_id = task.site_contact_id
    vendor_card_id = task.vendor_card_id
    vendor_contact_id = task.vendor_contact_id
    delete_task(db, task_id)
    logger.info("Task {} deleted by user {}", task_id, user.id)
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
        # VendorContact was deleted — return a safe non-blank acknowledgement.
        return HTMLResponse('<p class="text-xs text-gray-400">Task deleted.</p>')
    return HTMLResponse("")


@router.get("/v2/partials/tasks/{task_id}/edit-form", response_class=HTMLResponse)
async def task_edit_form(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit form for an existing CRM task (prefilled)."""
    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    is_vendor_task = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not is_vendor_task:
        raise HTTPException(400, "Not a CRM task")
    from app.services.task_service import _is_crm_task_authorized

    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    # Vendor task: resolve vendor_id (vendor_card_id direct, or via vendor_contact)
    if is_vendor_task:
        from app.models.vendors import VendorContact as _VendorContact

        vendor_id = task.vendor_card_id
        if not vendor_id and task.vendor_contact_id:
            vc = db.get(_VendorContact, task.vendor_contact_id)
            if vc:
                vendor_id = vc.vendor_card_id
        ctx = _base_ctx(request, user, "vendors")
        ctx["task"] = task
        ctx["vendor_id"] = vendor_id or 0
        return template_response("htmx/partials/vendors/tabs/_vendor_task_edit_form.html", ctx)
    # Resolve the real company_id: account task has it directly; for a contact task
    # we walk contact → site → company so the cancel button has a valid URL.
    real_company_id = task.company_id
    if not real_company_id and task.site_contact_id:
        contact = db.get(SiteContact, task.site_contact_id)
        if contact and contact.customer_site:
            real_company_id = contact.customer_site.company_id
    ctx = _base_ctx(request, user, "customers")
    ctx["task"] = task
    ctx["company_id"] = real_company_id or 0
    return template_response("htmx/partials/customers/_task_edit_form.html", ctx)


@router.post("/v2/partials/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task_endpoint(
    request: Request,
    task_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update title and/or due_at on a CRM task; return refreshed parent list.

    Authz: same gate as complete/delete — assignee, creator, account owner, or admin.
    """
    from datetime import timezone as _tz

    from app.services.task_service import (
        _is_crm_task_authorized,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    # Parse due_at: empty string → explicit clear (None); non-empty → parse.
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date format.</p>')
    # Set both controlled fields directly so an empty due_at clears the existing value.
    # (update_task skips None values to avoid mass-assignment; bypass that for explicit edits.)
    task.title = title.strip()
    task.due_at = due_dt
    db.commit()
    db.refresh(task)
    logger.info("Task {} edited by user {}", task_id, user.id)
    # Re-render the parent container
    task = db.get(RequisitionTask, task_id)
    company_id = task.company_id if task else None
    site_contact_id = task.site_contact_id if task else None
    vendor_card_id_edit = task.vendor_card_id if task else None
    vendor_contact_id_edit = task.vendor_contact_id if task else None
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id_edit:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id_edit)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id_edit
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id_edit:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id_edit)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    return HTMLResponse("")


@router.post("/v2/partials/tasks/{task_id}/snooze", response_class=HTMLResponse)
async def snooze_task_endpoint(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Push a CRM task's due_at forward by one week (or set to tomorrow if no due_at).

    Authz: same gate as edit/complete — assignee, creator, account owner, or admin.
    Returns the refreshed parent task list (account, contact, or vendor card).
    """
    from app.services.task_service import (
        _is_crm_task_authorized,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
        snooze_task,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to snooze this task")
    snooze_task(db, task_id)
    logger.info("Task {} snoozed by user {}", task_id, user.id)
    # Re-render the parent container (same logic as edit_task_endpoint)
    task = db.get(RequisitionTask, task_id)
    company_id = task.company_id if task else None
    site_contact_id = task.site_contact_id if task else None
    vendor_card_id_snooze = task.vendor_card_id if task else None
    vendor_contact_id_snooze = task.vendor_contact_id if task else None
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id_snooze:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id_snooze)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id_snooze
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id_snooze:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id_snooze)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    return HTMLResponse("")


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
    return template_response("htmx/partials/vendors/tabs/_vendor_task_form.html", ctx)


@router.post("/v2/partials/vendors/{vendor_id}/tasks", response_class=HTMLResponse)
async def create_vendor_task_endpoint(
    request: Request,
    vendor_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a vendor; return refreshed task list."""
    from datetime import date as _date
    from datetime import timezone as _tz

    from app.services.task_service import create_vendor_task, get_open_tasks_for_vendor_card

    vendor = get_vendor_card_or_404(db, vendor_id)
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = _date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_vendor_task(
        db,
        vendor_card_id=vendor_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
    )
    tasks = get_open_tasks_for_vendor_card(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["vendor"] = vendor
    ctx["vendor_tasks"] = tasks
    return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/activity/add-note-form",
    response_class=HTMLResponse,
)
async def activity_add_note_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-note form for the account Activity tab."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You are not allowed to add notes for this account")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    return template_response("htmx/partials/customers/_add_note_form.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/activity/add-note",
    response_class=HTMLResponse,
)
async def activity_add_note(
    request: Request,
    company_id: int,
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a company and return the refreshed Activity tab.

    A note does NOT advance the outbound cadence clock (cadence-neutral: direction=None
    → bump_clocks_from_activity early-returns without touching last_outbound_at).
    """
    from app.services.activity_service import log_company_note

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You are not allowed to add notes for this account")
    if not notes.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Note text is required.</p>')
    log_company_note(
        user_id=user.id,
        company_id=company_id,
        contact_name=None,
        notes=notes.strip(),
        db=db,
    )
    db.commit()
    # Re-render the full activity tab by delegating to the existing tab handler
    return await company_tab(
        request=request,
        company_id=company_id,
        tab="activity",
        site_id=None,
        user=user,
        db=db,
    )


# ── Vendor activity add-note ─────────────────────────────────────────────


@router.get(
    "/v2/partials/vendors/{vendor_id}/activity/add-note-form",
    response_class=HTMLResponse,
)
async def vendor_activity_add_note_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-note form for the vendor Activity tab."""
    vendor = get_vendor_card_or_404(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor.id
    return template_response("htmx/partials/vendors/_add_note_form.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/activity/add-note",
    response_class=HTMLResponse,
)
async def vendor_activity_add_note(
    request: Request,
    vendor_id: int,
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a vendor and return the refreshed Activity tab.

    Cadence-neutral: direction=None so bump_clocks_from_activity does not advance
    last_outbound_at.
    """
    from app.services.activity_service import log_vendor_note

    vendor = get_vendor_card_or_404(db, vendor_id)
    if not notes.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Note text is required.</p>')
    log_vendor_note(
        user_id=user.id,
        vendor_card_id=vendor.id,
        vendor_contact_id=None,
        contact_name=None,
        notes=notes.strip(),
        db=db,
        bump_last_activity=False,
    )
    db.commit()
    # Re-render the full activity tab by delegating to the existing tab handler
    return await vendor_tab(
        request=request,
        vendor_id=vendor_id,
        tab="activity",
        user=user,
        db=db,
    )
