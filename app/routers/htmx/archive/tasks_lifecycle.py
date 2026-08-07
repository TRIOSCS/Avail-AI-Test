"""Task lifecycle partials — complete, delete, inline edit form/save, snooze.

W4.8 split of the 969-line app/routers/htmx/archive.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__).

Called by: app/main.py (router mount via the package __init__).
Depends on: app.constants, app.database, app.dependencies, app.models,
    app.services.task_service, app.template_env, .._shared, .common
"""

from datetime import UTC, date, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....constants import UserRole
from ....database import get_db
from ....dependencies import require_user
from ....models import (
    RequisitionTask,
    SiteContact,
    User,
)
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router


@router.post("/v2/partials/tasks/{task_id}/complete", response_class=HTMLResponse)
async def complete_task_endpoint(
    request: Request,
    task_id: int,
    from_my_day: bool = False,
    completion_note: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a CRM task done (CRM account/contact or vendor card/contact). No activity
    log is created.

    Permissive auth: the caller only needs require_user — any logged-in user may mark
    a vendor task done (vendor tasks carry no ownership gate at complete time).

    An optional ``completion_note`` form field (a "how was this resolved?" note) is stored
    on the task when supplied — mirroring the part comms-tab complete path — instead of the
    endpoint silently discarding it. Empty submissions leave the note blank as before.

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
        complete_crm_task(
            db,
            task_id,
            user.id,
            completion_note=completion_note.strip(),
            is_admin=(user.role == UserRole.ADMIN),
        )
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
    from app.services.task_service import is_task_mutation_authorized

    # One gate for every task kind: creator, assignee, admin, or (customer tasks)
    # the parent account owner. Vendor delete is no longer admin-only.
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
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


def _render_task_edit_form(request: Request, user: User, db: Session, task: RequisitionTask, error: str | None = None):
    """Render the inline edit form (vendor or customer variant) for a CRM task.

    Shared by the GET edit-form route and the POST edit validation-error branches:
    the form is outerHTML-swapped over the #…-tasks-{id} container, so every
    response along the edit flow (including errors) must be this id-bearing
    fragment — a bare fragment would destroy the swap target and dead-end the
    widget.
    """
    is_vendor_task = task.vendor_card_id is not None or task.vendor_contact_id is not None
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
        ctx["error"] = error
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
    ctx["error"] = error
    return template_response("htmx/partials/customers/_task_edit_form.html", ctx)


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
    from app.services.task_service import is_task_mutation_authorized

    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    return _render_task_edit_form(request, user, db, task)


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

    from app.services.task_service import (
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
        is_task_mutation_authorized,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    # Validation errors re-render the id-bearing edit form (NOT a bare <p>): the
    # response outerHTML-swaps the #…-tasks-{id} container, which must survive.
    if not title.strip():
        return _render_task_edit_form(request, user, db, task, error="Title is required.")
    # Parse due_at: empty string → explicit clear (None); non-empty → parse.
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
        except ValueError:
            return _render_task_edit_form(request, user, db, task, error="Invalid date format.")
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
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
        is_task_mutation_authorized,
        snooze_task,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
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
