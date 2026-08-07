"""Per-vendor contact CRUD (add/edit/delete/set-primary) — HTMX parity P1.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_admin, require_user
from ....models import User
from ....template_env import template_response
from ....utils.column_limits import ensure_fits_column
from ....utils.vendor_helpers import sync_card_emails_on_contact_change
from ..._lookup_helpers import get_vendor_card_or_404
from .common import router

# ── Vendor Contact CRUD (HTMX, parity P1) ──────────────────────────────────


def _render_contact_row(request: Request, c, vendor):
    """Re-render a single vendor contact row partial."""
    return template_response(
        "htmx/partials/vendors/tabs/contact_row.html",
        {"request": request, "c": c, "vendor": vendor},
    )


def _render_contact_rows(request: Request, vendor, contacts):
    """Re-render the contact table rows only (tbody inner content).

    Used when a re-render must target #contacts-table-body with hx-swap="innerHTML"
    (e.g. set-primary flips the Primary badge across every row). Returning the full
    contacts.html shell here would nest a <div>/<form>/<table> inside the <tbody>.
    """
    return template_response(
        "htmx/partials/vendors/tabs/contact_rows.html",
        {"request": request, "vendor": vendor, "contacts": contacts},
    )


@router.post("/v2/partials/vendors/{vendor_id}/contacts", response_class=HTMLResponse)
async def vendor_contact_add(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a vendor contact (HTMX).

    require_user gate — mirrors vendor edit.
    """
    from ....models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "email is required")
    full_name = (form.get("full_name") or "").strip()
    title = (form.get("title") or "").strip()
    phone = (form.get("phone") or "").strip()

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    for _field, _value, _label in (
        ("email", email, "Email"),
        ("full_name", full_name, "Name"),
        ("title", title, "Title"),
        ("phone", phone, "Phone"),
    ):
        ensure_fits_column(VC, _field, _value or None, _label)

    # Deduplicate by (vendor_card_id, email)
    existing = db.query(VC).filter(VC.vendor_card_id == vendor_id, VC.email == email).first()
    if existing:
        raise HTTPException(409, "A contact with that email already exists")

    vc = VC(
        vendor_card_id=vendor_id,
        email=email,
        full_name=full_name or None,
        title=title or None,
        phone=phone or None,
        contact_type="individual" if full_name else "company",
        source="manual",
        is_verified=True,
        confidence=100,
        is_primary=False,
    )
    db.add(vc)
    # Mirror into the legacy card.emails[] reachability array — shared helper with the
    # JSON API (app/routers/vendor_contacts.py), Wave 3 item 5.
    sync_card_emails_on_contact_change(vendor, None, email)
    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} added to vendor {} by {}", vc.id, vendor_id, user.email)
    return _render_contact_row(request, vc, vendor)


@router.put("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def vendor_contact_edit(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Edit a vendor contact (HTMX).

    require_user gate.
    """
    from ....models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    full_name = (form.get("full_name") or "").strip()
    title = (form.get("title") or "").strip()
    email = (form.get("email") or "").strip()
    phone = (form.get("phone") or "").strip()

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    for _field, _value, _label in (
        ("email", email, "Email"),
        ("full_name", full_name, "Name"),
        ("title", title, "Title"),
        ("phone", phone, "Phone"),
    ):
        ensure_fits_column(VC, _field, _value or None, _label)

    old_email = vc.email
    if full_name:
        vc.full_name = full_name
        vc.contact_type = "individual"
    if title:
        vc.title = title
    if email and email != vc.email:
        collision = db.query(VC).filter(VC.vendor_card_id == vendor_id, VC.email == email, VC.id != contact_id).first()
        if collision:
            raise HTTPException(409, "Another contact already has that email")
        vc.email = email
    if phone:
        vc.phone = phone

    # Mirror the address change into card.emails[] — shared helper, Wave 3 item 5.
    sync_card_emails_on_contact_change(vendor, old_email, vc.email)
    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} updated by {}", contact_id, user.email)
    return _render_contact_row(request, vc, vendor)


@router.delete("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def vendor_contact_delete(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor contact (HTMX).

    require_admin gate — matches vendor delete auth.
    """
    from ....models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    # Remove the address from card.emails[] — shared helper, Wave 3 item 5.
    sync_card_emails_on_contact_change(vendor, vc.email, None)
    db.delete(vc)
    db.commit()
    logger.info("VendorContact {} deleted by {}", contact_id, user.email)
    return HTMLResponse("")  # HTMX deletes the row via hx-swap="outerHTML"


@router.post(
    "/v2/partials/vendors/{vendor_id}/contacts/{contact_id}/set-primary",
    response_class=HTMLResponse,
)
async def vendor_contact_set_primary(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a contact as primary; clears is_primary on all other contacts for this
    vendor."""
    from ....models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    # Clear all primaries for this vendor, then set this one
    db.query(VC).filter(VC.vendor_card_id == vendor_id).update({"is_primary": False})
    vc.is_primary = True
    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} set as primary by {}", contact_id, user.email)

    contacts = (
        db.query(VC)
        .filter(VC.vendor_card_id == vendor_id)
        .order_by(VC.interaction_count.desc().nullslast())
        .limit(50)
        .all()
    )
    # Return rows only — the button swaps innerHTML into <tbody id="contacts-table-body">.
    return _render_contact_rows(request, vendor, contacts)
