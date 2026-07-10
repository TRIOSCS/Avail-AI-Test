"""routers/htmx/companies/merge.py — company + contact duplicate-merge flows (P4.3
split).

Merge-preview → confirm → merge for both companies and site contacts. No FK
reassignment logic lives here — that's owned by the canonical
``company_merge_service.merge_companies`` / ``contact_merge_service.merge_contacts``
engines; these routes only gate authz, show counts, and call into the engine.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration)
Depends on: app.services.company_merge_service, app.services.contact_merge_service,
    app.models.intelligence, app.models.task, app.models.crm, .contacts
    (_render_contacts_list)
"""

import html as html_mod
import json

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import can_manage_account, require_user
from ....models import Company, CustomerSite, Requisition, SiteContact, User
from ....models.crm import SiteContactAttachment
from ....models.intelligence import ActivityLog
from ....models.task import RequisitionTask
from ....services.company_merge_service import merge_companies
from ....services.contact_merge_service import merge_contacts
from ....template_env import template_response
from ....utils.sql_helpers import escape_like
from . import router
from .contacts import _render_contacts_list

# ── Merge Duplicate ─────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/merge-preview", response_class=HTMLResponse)
async def company_merge_preview(
    request: Request,
    company_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into company_id.

    Shows counts: sites, contacts, activities that will be reassigned; confirms the
    loser will be deleted. Used by the merge-confirm modal before the user commits.
    """
    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")
    if not can_manage_account(user, remove, db):
        raise HTTPException(403, "Not authorized to manage the duplicate company")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a company with itself")

    # Count what will move
    site_count = db.query(sqlfunc.count(CustomerSite.id)).filter(CustomerSite.company_id == remove.id).scalar() or 0
    contact_count = (
        db.query(sqlfunc.count(SiteContact.id))
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(CustomerSite.company_id == remove.id)
        .scalar()
        or 0
    )
    activity_count = db.query(sqlfunc.count(ActivityLog.id)).filter(ActivityLog.company_id == remove.id).scalar() or 0
    req_count = db.query(sqlfunc.count(Requisition.id)).filter(Requisition.company_id == remove.id).scalar() or 0

    ctx = {
        "request": request,
        "keep": keep,
        "remove": remove,
        "site_count": site_count,
        "contact_count": contact_count,
        "activity_count": activity_count,
        "req_count": req_count,
    }
    return template_response("htmx/partials/customers/_merge_preview.html", ctx)


@router.post("/v2/partials/customers/{company_id}/merge", response_class=HTMLResponse)
async def company_merge(
    request: Request,
    company_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into company_id (the keeper).

    Requires confirmed="true" to prevent accidental submissions. Calls the
    canonical merge_companies() engine — no FK logic lives here.
    POST is mandatory: this is a destructive, irreversible operation.
    """
    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if remove_id == company_id:
        raise HTTPException(400, "Cannot merge a company with itself")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")
    if not can_manage_account(user, remove, db):
        raise HTTPException(403, "Not authorized to manage the duplicate company")

    try:
        result = merge_companies(company_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "Manual company merge: kept {} ({}), removed {} by {}",
        company_id,
        keep.name,
        remove_id,
        user.email,
    )

    # Redirect browser to the keeper's FULL-PAGE detail URL via HTMX redirect header.
    # HX-Redirect triggers a real window.location navigation, so it must point at the
    # base-page-wrapped route (/v2/customers/{id}) — NOT the bare partial
    # (/v2/partials/customers/{id}), which would land the user on an unstyled fragment
    # with no nav shell or CSS entry point.
    safe_name = html_mod.escape(keep.name or "")
    response = HTMLResponse(
        f'<p class="text-sm text-emerald-600 py-2">Merged into <strong>{safe_name}</strong>. '
        f"{int(result.get('sites_moved', 0))} site(s) and {int(result.get('reassigned', 0))} record(s) reassigned.</p>",
        status_code=200,
    )
    response.headers["HX-Redirect"] = f"/v2/customers/{company_id}"
    return response


@router.get("/v2/partials/customers/{company_id}/merge-form", response_class=HTMLResponse)
async def company_merge_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a company.

    Renders a search input to find the duplicate and a submit button that triggers the
    merge-preview step.
    """
    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    ctx = {"request": request, "keep": keep}
    return template_response("htmx/partials/customers/_merge_form.html", ctx)


# ── Contact Merge Duplicate ──────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/contacts/search", response_class=HTMLResponse)
async def contact_search_typeahead(
    request: Request,
    company_id: int,
    q: str = "",
    exclude_id: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return contacts for a company as clickable typeahead results.

    Used by the contact merge form to pick the "loser" contact. Excludes the keeper
    (exclude_id=) so a contact cannot be merged with itself.
    """
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company or not can_manage_account(user, company, db):
        return HTMLResponse("")

    contacts = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.id != exclude_id,
            SiteContact.full_name.ilike(f"%{escape_like(q.strip())}%", escape="\\"),
        )
        .order_by(SiteContact.full_name)
        .limit(10)
        .all()
    )
    rows = [
        f'<button type="button" data-contact-id="{c.id}" '
        f'class="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50">'
        f"{html_mod.escape(c.full_name or '')}"
        f"{'  (' + html_mod.escape(c.email) + ')' if c.email else ''}"
        f"</button>"
        for c in contacts
    ]
    return HTMLResponse("\n".join(rows))


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-form",
    response_class=HTMLResponse,
)
async def contact_merge_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a contact."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    return template_response(
        "htmx/partials/customers/_contact_merge_form.html",
        {"request": request, "keep": keep, "company": company},
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-preview",
    response_class=HTMLResponse,
)
async def contact_merge_preview(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into contact_id."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    remove = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == remove_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not remove:
        raise HTTPException(400, "Duplicate contact not found or not in this company")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    activity_count = (
        db.query(sqlfunc.count(ActivityLog.id)).filter(ActivityLog.site_contact_id == remove.id).scalar() or 0
    )
    task_count = (
        db.query(sqlfunc.count(RequisitionTask.id)).filter(RequisitionTask.site_contact_id == remove.id).scalar() or 0
    )
    attachment_count = (
        db.query(sqlfunc.count(SiteContactAttachment.id))
        .filter(SiteContactAttachment.site_contact_id == remove.id)
        .scalar()
        or 0
    )

    return template_response(
        "htmx/partials/customers/_contact_merge_preview.html",
        {
            "request": request,
            "keep": keep,
            "remove": remove,
            "company": company,
            "activity_count": activity_count,
            "task_count": task_count,
            "attachment_count": attachment_count,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge",
    response_class=HTMLResponse,
)
async def contact_merge(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into contact_id (the keeper).

    Requires confirmed="true". Calls merge_contacts() — no FK logic here.
    """
    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if remove_id == contact_id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    remove = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == remove_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not remove:
        raise HTTPException(400, "Duplicate contact not found or not in this company")

    try:
        result = merge_contacts(contact_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    reassigned = int(result.get("reassigned", 0))
    logger.info(
        "Manual contact merge: kept {} ({}), removed {} by {}",
        contact_id,
        keep.full_name,
        remove_id,
        user.email,
    )

    # Return the refreshed contacts grouped-list (targeted at #contacts-tab-list) so the
    # merged-away contact disappears immediately; the preview form's after-request closes
    # the modal. Mirrors the sibling Move-contact flow — no in-modal dead-end.
    response = _render_contacts_list(request, user, company, db)
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": f"Contact merged — {reassigned} record(s) reassigned", "type": "success"}}
    )
    return response
