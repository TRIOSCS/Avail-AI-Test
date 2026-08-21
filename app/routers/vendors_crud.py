"""routers/vendors_crud.py — Vendor Card CRUD & Review endpoints.

Handles vendor listing, search, duplicate checking, update, blacklist,
delete, and vendor review management.

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_utils, vendor_helpers, cache
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import Company, Offer, User, VendorCard, VendorReview
from ..schemas.responses import VendorDetailResponse, VendorListResponse
from ..schemas.vendors import VendorBlacklistToggle, VendorCardCreate, VendorCardUpdate, VendorReviewCreate

# The duplicate-check logic lives in the service (shared with the sightings RFQ
# composer's POST composer-vendor endpoint). _fuzzy_match_python is re-exported
# for back-compat — existing callers/tests import it from this module.
from ..services.vendor_analysis_service import list_vendor_cards
from ..services.vendor_duplicates import _fuzzy_match_python  # noqa: F401
from ..services.vendor_duplicates import check_vendor_duplicate as _check_vendor_duplicate
from ..utils.search_builder import SearchBuilder
from ..utils.vendor_helpers import card_to_dict, find_vendor_card_by_name
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["vendors"])


@router.get("/api/vendors/check-duplicate")
async def check_vendor_duplicate(
    name: str = Query(..., min_length=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check for duplicate vendors by name (exact + fuzzy).

    Returns exact and fuzzy matches (threshold 80 for suggestions). Used by frontend
    before vendor creation to warn about duplicates. Uses PostgreSQL pg_trgm trigram
    index when available, falls back to Python-side rapidfuzz matching on SQLite. Thin
    wrapper over services.vendor_duplicates.check_vendor_duplicate.
    """
    return {"matches": _check_vendor_duplicate(name, db)}


@router.post("/api/vendors", status_code=201)
async def create_vendor(
    data: VendorCardCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new VendorCard.

    Returns 201 on success. Returns 409 if a vendor with the same normalized name
    already exists. Returns 422 if the display_name is missing or blank.
    """
    norm = normalize_vendor_name(data.display_name)
    existing = find_vendor_card_by_name(data.display_name, db)
    if existing:
        raise HTTPException(
            409,
            f"Vendor '{existing.display_name}' already exists (ID {existing.id})",
        )

    card = VendorCard(
        normalized_name=norm,
        display_name=data.display_name,
        website=data.website or None,
        emails=data.emails or [],
        phones=data.phones or [],
        industry=data.industry or None,
        hq_city=data.hq_city or None,
        hq_country=data.hq_country or None,
        employee_size=data.employee_size or None,
        source="manual",
        is_blacklisted=False,
        is_new_vendor=True,
        sighting_count=0,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    from loguru import logger

    logger.info("VendorCard {} created by {}", card.id, user.email)
    return card_to_dict(card, db)


@router.get("/api/vendors", response_model=VendorListResponse, response_model_exclude_none=True)
async def list_vendors(
    q: str = Query("", description="Vendor name search filter"),
    tag: str = Query("", description="Filter by brand or commodity tag"),
    tier: str = Query("", description="Filter by tier: proven, developing, caution, new"),
    sort: str = Query("", description="Sort column: name, score, sighting_count, response_rate, total_pos"),
    order: str = Query("asc", description="Sort direction: asc or desc"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List vendor cards with search, pagination, tier filter, sort, and engagement
    scores."""
    q = q.strip().lower()
    return list_vendor_cards(q=q, tag=tag, tier=tier, sort=sort, order=order, limit=limit, offset=offset, db=db)


@router.get("/api/autocomplete/names")
async def autocomplete_names(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lightweight name autocomplete across VendorCards and Companies."""
    q = request.query_params.get("q", "").strip().lower()
    if len(q) < 2:
        return []
    try:
        limit = min(int(request.query_params.get("limit", "8")), 20)
    except (ValueError, TypeError):
        limit = 8
    sb = SearchBuilder(q)

    from sqlalchemy import String, cast

    # Primary: match on normalized_name
    vendors_by_name = (
        db.query(VendorCard)
        .filter(VendorCard.normalized_name.ilike(f"%{sb.safe}%", escape="\\"))
        .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
        .limit(limit)
        .all()
    )

    # Secondary: match on alternate_names JSON (cast to text for ILIKE)
    seen_ids = {v.id for v in vendors_by_name}
    vendors_by_alt = (
        db.query(VendorCard)
        .filter(
            cast(VendorCard.alternate_names, String).ilike(f"%{sb.safe}%", escape="\\"),
            VendorCard.id.notin_(seen_ids) if seen_ids else True,
        )
        .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
        .limit(limit)
        .all()
    )

    companies = (
        db.query(Company.id, Company.name)
        .filter(Company.is_active, Company.name.ilike(f"%{sb.safe}%", escape="\\"))
        .order_by(Company.name)
        .limit(limit)
        .all()
    )

    results = []
    for v in vendors_by_name + vendors_by_alt:
        results.append({"id": v.id, "name": v.display_name, "type": "vendor"})
    for c in companies:
        results.append({"id": c.id, "name": c.name, "type": "customer"})
    # Vendors first (by sighting_count already), then customers
    return results[:limit]


@router.get("/api/vendors/{card_id}", response_model=VendorDetailResponse, response_model_exclude_none=True)
async def get_vendor(card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get vendor card detail with reviews, contacts, and engagement metrics."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    return card_to_dict(card, db)


@router.put("/api/vendors/{card_id}")
async def update_vendor(
    card_id: int,
    data: VendorCardUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    if data.emails is not None:
        card.emails = data.emails
    if data.phones is not None:
        card.phones = data.phones
    if data.website is not None:
        card.website = data.website
    if data.display_name is not None and data.display_name.strip():
        card.display_name = data.display_name.strip()
    if data.is_blacklisted is not None:
        card.is_blacklisted = data.is_blacklisted
    db.commit()
    return card_to_dict(card, db)


@router.post("/api/vendors/{card_id}/blacklist")
async def toggle_blacklist(
    card_id: int,
    data: VendorBlacklistToggle,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle vendor blacklist status."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    card.is_blacklisted = data.blacklisted if data.blacklisted is not None else (not card.is_blacklisted)
    db.commit()
    return card_to_dict(card, db)


@router.delete("/api/vendors/{card_id}")
async def delete_vendor(card_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    active_offers = db.query(Offer).filter(Offer.vendor_card_id == card.id).count()
    if active_offers > 0:
        raise HTTPException(400, f"Cannot delete vendor with {active_offers} active offers. Archive instead.")
    db.delete(card)
    db.commit()
    return {"ok": True}


# -- Vendor Reviews -----------------------------------------------------------


@router.post("/api/vendors/{card_id}/reviews")
async def add_review(
    card_id: int,
    payload: VendorReviewCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    review = VendorReview(
        vendor_card_id=card.id,
        user_id=user.id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(review)
    db.commit()
    return card_to_dict(card, db)


@router.delete("/api/vendors/{card_id}/reviews/{review_id}")
async def delete_review(
    card_id: int,
    review_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    review = db.query(VendorReview).filter_by(id=review_id, vendor_card_id=card_id, user_id=user.id).first()
    if not review:
        raise HTTPException(404, "Review not found or not yours")
    db.delete(review)
    db.commit()
    card = db.get(VendorCard, card_id)
    if not card:
        return {"ok": True}
    return card_to_dict(card, db)
