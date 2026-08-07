"""Activity add-note partials for accounts and vendors (form + post).

W4.8 split of the 969-line app/routers/htmx/archive.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__).

Called by: app/main.py (router mount via the package __init__).
Depends on: app.database, app.dependencies, app.models,
    app.services.activity_service, app.template_env, ..._lookup_helpers,
    .._shared, .._shared_tabs (company_tab, vendor_tab)
"""

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import (
    can_manage_account,
    require_user,
)
from ....models import Company, User
from ....template_env import template_response
from ..._lookup_helpers import get_vendor_card_or_404
from .._shared import _base_ctx
from .._shared_tabs import company_tab, vendor_tab
from .common import router


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
