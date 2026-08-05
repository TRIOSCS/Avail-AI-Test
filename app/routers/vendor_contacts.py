"""routers/vendor_contacts.py — Structured vendor contact CRUD.

Handles the structured VendorContact CRUD endpoints (bulk list, per-vendor
list, add, update, delete, log-call).

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_helpers
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, Direction, EventType
from ..database import get_db
from ..dependencies import require_buyer, require_user
from ..models import User, VendorCard, VendorContact
from ..schemas.vendors import VendorContactCreate, VendorContactUpdate
from ..template_env import template_response
from ..utils.column_limits import ensure_fits_column
from ..utils.phone_utils import format_phone_e164
from ..utils.vendor_helpers import sync_card_emails_on_contact_change

router = APIRouter(tags=["vendors"])


# -- Structured Vendor Contact CRUD -------------------------------------------


@router.get("/api/vendor-contacts/bulk")
async def bulk_vendor_contacts(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All vendor contacts in a single query -- replaces N+1 per-vendor fetches."""
    from sqlalchemy.orm import joinedload as jl

    query = (
        db.query(VendorContact)
        .join(VendorCard)
        .filter(VendorCard.is_blacklisted == False)  # noqa: E712
        .options(jl(VendorContact.vendor_card))
        .order_by(VendorContact.id)
    )
    total = query.count()
    if offset and offset >= total:
        # Stale offset beyond the result set — snap back to page 1 with the real
        # total (same clamp as crm_service.customer_contacts_context).
        offset = 0
    contacts = query.offset(offset).limit(limit).all()
    items = [
        {
            "id": c.id,
            "vendor_id": c.vendor_card_id,
            "vendor_name": c.vendor_card.display_name if c.vendor_card else "Unknown",
            "contact_type": c.contact_type,
            "full_name": c.full_name,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "title": c.title,
            "label": c.label,
            "email": c.email,
            "phone": c.phone,
            "phone_mobile": c.phone_mobile,
            "source": c.source,
            "is_verified": c.is_verified,
            "confidence": c.confidence,
            "interaction_count": c.interaction_count,
            "relationship_score": c.relationship_score,
            "activity_trend": c.activity_trend,
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
        }
        for c in contacts
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/vendors/{card_id}/contacts")
async def list_vendor_contacts(card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """List all structured contacts for a vendor card."""
    contacts = (
        db.query(VendorContact)
        .filter_by(vendor_card_id=card_id)
        .order_by(VendorContact.confidence.desc(), VendorContact.last_seen_at.desc())
        .all()
    )
    return [
        {
            "id": c.id,
            "contact_type": c.contact_type,
            "full_name": c.full_name,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "title": c.title,
            "label": c.label,
            "email": c.email,
            "phone": c.phone,
            "phone_mobile": c.phone_mobile,
            "phone_type": c.phone_type,
            "source": c.source,
            "is_verified": c.is_verified,
            "confidence": c.confidence,
            "interaction_count": c.interaction_count,
            "relationship_score": c.relationship_score,
            "activity_trend": c.activity_trend,
            "score_computed_at": c.score_computed_at.isoformat() if c.score_computed_at else None,
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
        }
        for c in contacts
    ]


@router.post("/api/vendors/{card_id}/contacts/{contact_id}/log-call")
async def log_contact_call(
    request: Request,
    card_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-call event for a vendor contact and return the refreshed row.

    Re-renders the contact row so the interaction count visibly ticks up, and attaches
    an ``HX-Trigger: showToast`` header so the click is explicitly acknowledged. The
    button previously used ``hx-swap="none"`` against a bare-JSON response, so the user
    got zero on-screen feedback (no toast, no count change).
    """
    from ..models import ActivityLog

    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")
    card = db.get(VendorCard, card_id)

    now = datetime.now(UTC)
    activity = ActivityLog(
        user_id=user.id,
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.PHONE,
        direction=Direction.OUTBOUND,
        event_type=EventType.CALL,
        is_meaningful=True,
        vendor_card_id=card_id,
        vendor_contact_id=contact_id,
        contact_phone=vc.phone or vc.phone_mobile,
        contact_name=vc.full_name,
        auto_logged=True,
        occurred_at=now,
        created_at=now,
    )
    db.add(activity)

    db.query(VendorCard).filter(VendorCard.id == card_id).update({"last_activity_at": now}, synchronize_session=False)

    vc.interaction_count = (vc.interaction_count or 0) + 1
    vc.last_interaction_at = now
    vc.last_seen_at = now

    db.commit()
    db.refresh(vc)

    resp = template_response(
        "htmx/partials/vendors/tabs/contact_row.html",
        {"request": request, "c": vc, "vendor": card},
    )
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "Call logged", "type": "success"}})
    return resp


@router.post("/api/vendors/{card_id}/contacts")
async def add_vendor_contact(
    card_id: int,
    payload: VendorContactCreate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Manually add a structured contact to a vendor card."""
    email = payload.email

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    # phone is guarded raw: E.164 formatting only ever shortens/normalizes.
    for _field, _value, _label in (
        ("email", email, "Email"),
        ("full_name", payload.full_name, "Name"),
        ("title", payload.title, "Title"),
        ("label", payload.label, "Label"),
        ("phone", payload.phone, "Phone"),
    ):
        ensure_fits_column(VendorContact, _field, _value, _label)

    card = db.query(VendorCard).filter_by(id=card_id).first()
    if not card:
        raise HTTPException(404, "Vendor card not found")

    # Check for duplicate
    existing = db.query(VendorContact).filter_by(vendor_card_id=card_id, email=email).first()
    if existing:
        return {
            "id": existing.id,
            "message": "Contact already exists",
            "duplicate": True,
        }

    phone = format_phone_e164(payload.phone) or payload.phone if payload.phone else None
    vc = VendorContact(
        vendor_card_id=card_id,
        email=email,
        full_name=payload.full_name,
        title=payload.title,
        label=payload.label,
        phone=phone,
        contact_type="individual" if payload.full_name else "company",
        source="manual",
        is_verified=True,
        confidence=100,
    )
    db.add(vc)

    # Also add to legacy emails[] for backward compat — shared helper with the HTMX
    # endpoints (app/routers/htmx/vendors.py), Wave 3 item 5.
    sync_card_emails_on_contact_change(card, None, email)

    db.commit()
    return {"id": vc.id, "message": "Contact added", "duplicate": False}


@router.put("/api/vendors/{card_id}/contacts/{contact_id}")
async def update_vendor_contact(
    card_id: int,
    contact_id: int,
    payload: VendorContactUpdate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Update a structured vendor contact."""
    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    for _field, _value, _label in (
        ("email", payload.email, "Email"),
        ("full_name", payload.full_name, "Name"),
        ("title", payload.title, "Title"),
        ("label", payload.label, "Label"),
        ("phone", payload.phone, "Phone"),
    ):
        ensure_fits_column(VendorContact, _field, _value, _label)

    old_email = vc.email

    if payload.full_name is not None:
        vc.full_name = payload.full_name
        vc.contact_type = "individual" if payload.full_name else "company"
    if payload.title is not None:
        vc.title = payload.title
    if payload.email is not None and payload.email != old_email:
        existing = db.query(VendorContact).filter_by(vendor_card_id=card_id, email=payload.email).first()
        if existing and existing.id != contact_id:
            raise HTTPException(409, "Another contact already has this email")
        vc.email = payload.email
    if payload.label is not None:
        vc.label = payload.label
    if payload.phone is not None:
        vc.phone = format_phone_e164(payload.phone) or payload.phone

    vc.last_seen_at = datetime.now(UTC)

    # Sync legacy emails[] array — shared helper, Wave 3 item 5.
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if card:
        sync_card_emails_on_contact_change(card, old_email, vc.email)

    db.commit()
    return {"ok": True, "id": vc.id}


@router.delete("/api/vendors/{card_id}/contacts/{contact_id}")
async def delete_vendor_contact(
    card_id: int,
    contact_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Delete a structured vendor contact."""
    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")
    # Remove from legacy emails[] too — shared helper, Wave 3 item 5.
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if card:
        sync_card_emails_on_contact_change(card, vc.email, None)
    db.delete(vc)
    db.commit()
    return {"ok": True}
