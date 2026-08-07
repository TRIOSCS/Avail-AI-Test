"""Vendor ownership (claim/release/badge), custom fields, and reviews.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ....database import get_db
from ....dependencies import require_user
from ....models import User
from ....template_env import template_response
from ..._lookup_helpers import get_vendor_card_or_404
from .common import router

# ── Vendor Ownership UI (surface existing StrategicVendor) ─────────────────


@router.post("/v2/partials/vendors/{vendor_id}/claim", response_class=HTMLResponse)
async def vendor_claim(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim this vendor as strategic for the current user."""
    from ....services.strategic_vendor_service import claim_vendor, get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    _record, error = claim_vendor(db, user.id, vendor_id)
    if error:
        raise HTTPException(400, error)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


@router.post("/v2/partials/vendors/{vendor_id}/release", response_class=HTMLResponse)
async def vendor_release(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Release this vendor from the current user's strategic list."""
    from ....services.strategic_vendor_service import drop_vendor, get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    _ok, error = drop_vendor(db, user.id, vendor_id)
    if error:
        raise HTTPException(400, error)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


def _render_vendor_ownership_badge(request: Request, vendor_id: int, status, user):
    """Render the vendor ownership badge partial."""
    return template_response(
        "htmx/partials/vendors/_ownership_badge.html",
        {"request": request, "vendor_id": vendor_id, "ownership": status, "user": user},
    )


@router.get("/v2/partials/vendors/{vendor_id}/ownership", response_class=HTMLResponse)
async def vendor_ownership_badge(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the strategic ownership badge for a vendor (lazy-loaded)."""
    from ....services.strategic_vendor_service import get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


# ── Vendor Custom Fields (parity P1) ───────────────────────────────────────


def _render_vendor_custom_fields(request: Request, vendor):
    """Render vendor _custom_fields partial."""
    return template_response(
        "htmx/partials/vendors/_custom_fields.html",
        {"request": request, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/custom-fields", response_class=HTMLResponse)
async def vendor_add_custom_field(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a custom field on a vendor card.

    require_user gate.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from ....models.vendors import VendorCard as VC

    vendor = db.get(VC, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    form = await request.form()
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")

    existing = vendor.custom_fields or {}
    updated = {**existing, label: value}
    try:
        vendor.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    flag_modified(vendor, "custom_fields")
    db.commit()
    db.refresh(vendor)
    logger.info("Vendor {} custom field '{}' set by {}", vendor_id, label, user.email)
    return _render_vendor_custom_fields(request, vendor)


@router.delete(
    "/v2/partials/vendors/{vendor_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def vendor_delete_custom_field(
    request: Request,
    vendor_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a custom field from a vendor card.

    require_user gate.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from ....models.vendors import VendorCard as VC

    vendor = db.get(VC, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    existing = dict(vendor.custom_fields or {})
    existing.pop(label, None)
    vendor.custom_fields = existing
    flag_modified(vendor, "custom_fields")
    db.commit()
    db.refresh(vendor)
    logger.info("Vendor {} custom field '{}' removed by {}", vendor_id, label, user.email)
    return _render_vendor_custom_fields(request, vendor)


@router.get("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def vendor_reviews(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return reviews section for a vendor."""
    from ....models import VendorReview

    vendor = get_vendor_card_or_404(db, vendor_id)

    reviews = (
        db.query(VendorReview)
        .filter(VendorReview.vendor_card_id == vendor_id)
        .options(joinedload(VendorReview.user))
        .order_by(VendorReview.created_at.desc())
        .limit(20)
        .all()
    )
    return template_response(
        "htmx/partials/vendors/reviews.html",
        {"request": request, "reviews": reviews, "vendor": vendor, "user": user},
    )


@router.post("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def add_vendor_review(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a review to a vendor and return refreshed reviews."""
    from ....models import VendorReview

    get_vendor_card_or_404(db, vendor_id)  # validates existence

    form = await request.form()
    try:
        rating = int(form.get("rating", "3"))
    except (ValueError, TypeError):
        rating = 3
    comment = form.get("comment", "").strip()

    review = VendorReview(
        vendor_card_id=vendor_id,
        user_id=user.id,
        rating=max(1, min(5, rating)),
        comment=comment or None,
    )
    db.add(review)
    db.commit()
    logger.info("Review added for vendor {} by {} (rating={})", vendor_id, user.email, rating)

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)


@router.delete("/v2/partials/vendors/{vendor_id}/reviews/{review_id}", response_class=HTMLResponse)
async def delete_vendor_review(
    request: Request,
    vendor_id: int,
    review_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a vendor review (own reviews only) and return refreshed reviews."""
    from ....models import VendorReview

    review = (
        db.query(VendorReview).filter(VendorReview.id == review_id, VendorReview.vendor_card_id == vendor_id).first()
    )
    if not review:
        raise HTTPException(404, "Review not found")
    if review.user_id != user.id:
        raise HTTPException(403, "Can only delete your own reviews")

    db.delete(review)
    db.commit()

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)
