"""
routers/vendors_crud.py — Vendor Card CRUD & Review endpoints.

Handles vendor listing, search, duplicate checking, update, blacklist,
delete, and vendor review management.

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_utils, vendor_helpers, cache
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func as sqlfunc
from sqlalchemy import text as sqltext
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import Company, User, VendorCard, VendorReview
from ..schemas.responses import VendorDetailResponse, VendorListResponse
from ..schemas.vendors import VendorBlacklistToggle, VendorCardUpdate, VendorReviewCreate
from ..utils.sql_helpers import escape_like
from ..utils.vendor_helpers import card_to_dict
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["vendors"])


@router.get("/api/vendors/check-duplicate")
async def check_vendor_duplicate(
    name: str = Query(..., min_length=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check for duplicate vendors by name (exact + fuzzy).

    Returns exact and fuzzy matches (threshold 80 for suggestions).
    Used by frontend before vendor creation to warn about duplicates.
    """
    norm = normalize_vendor_name(name)
    matches = []

    # Exact match
    exact = db.query(VendorCard).filter_by(normalized_name=norm).first()
    if exact:
        matches.append(
            {
                "id": exact.id,
                "name": exact.display_name,
                "match": "exact",
                "score": 100,
            }
        )
        return {"matches": matches}

    # Fuzzy matches
    try:
        from thefuzz import fuzz

        existing = db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.display_name).limit(500).all()
        for row in existing:
            score = fuzz.token_sort_ratio(norm, row.normalized_name)
            if score >= 80:
                matches.append(
                    {
                        "id": row.id,
                        "name": row.display_name,
                        "match": "fuzzy",
                        "score": score,
                    }
                )
        matches.sort(key=lambda m: m["score"], reverse=True)
    except ImportError:  # pragma: no cover
        pass

    return {"matches": matches[:5]}


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
    """List vendor cards with search, pagination, tier filter, sort, and engagement scores."""

    @cached_endpoint(prefix="vendor_list", ttl_hours=0.5, key_params=["q", "tag", "tier", "sort", "order", "limit", "offset"])
    def _fetch(q, tag, tier, sort, order, limit, offset, db):
        query = db.query(VendorCard)

        # ── Tier filter ──
        if tier:
            tier = tier.strip().lower()
            if tier == "proven":
                query = query.filter(
                    VendorCard.vendor_score.isnot(None),
                    VendorCard.vendor_score >= 66,
                    VendorCard.is_new_vendor.is_(False),
                )
            elif tier == "developing":
                query = query.filter(
                    VendorCard.vendor_score.isnot(None),
                    VendorCard.vendor_score >= 33,
                    VendorCard.vendor_score < 66,
                    VendorCard.is_new_vendor.is_(False),
                )
            elif tier == "caution":
                query = query.filter(
                    VendorCard.vendor_score.isnot(None),
                    VendorCard.vendor_score < 33,
                    VendorCard.is_new_vendor.is_(False),
                )
            elif tier == "new":
                query = query.filter(
                    sqlfunc.coalesce(VendorCard.is_new_vendor, True).is_(True)
                    | VendorCard.vendor_score.is_(None)
                )

        # ── Default order ──
        query = query.order_by(VendorCard.display_name)
        if tag.strip():
            from sqlalchemy import String as SAString

            safe_tag = tag.strip().lower()
            query = query.filter(
                sqlfunc.lower(sqlfunc.cast(VendorCard.brand_tags, SAString)).contains(safe_tag)
                | sqlfunc.lower(sqlfunc.cast(VendorCard.commodity_tags, SAString)).contains(safe_tag)
            )
        if q:
            if len(q) >= 3:
                # Full-text search for longer queries (faster + ranked)
                try:
                    fts_query = (
                        db.query(VendorCard)
                        .filter(
                            VendorCard.search_vector.isnot(None),
                            sqltext("search_vector @@ plainto_tsquery('english', :q)"),
                        )
                        .params(q=q)
                        .order_by(
                            sqltext("ts_rank(search_vector, plainto_tsquery('english', :q)) DESC"),
                        )
                        .params(q=q)
                    )
                    fts_count = fts_query.count()
                    if fts_count > 0:
                        query = fts_query
                    else:
                        # FTS found nothing, fall back to ILIKE
                        safe_q = escape_like(q)
                        query = query.filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
                except (ProgrammingError, OperationalError):
                    # FTS not available (e.g., SQLite in tests), fall back to ILIKE
                    safe_q = escape_like(q)
                    query = query.filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
            else:
                safe_q = escape_like(q)
                query = query.filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
        # ── Apply explicit sort (overrides default order_by) ──
        if sort:
            sort = sort.strip().lower()
            sort_map = {
                "name": VendorCard.display_name,
                "score": VendorCard.vendor_score,
                "sighting_count": VendorCard.sighting_count,
                "response_rate": VendorCard.total_responses,  # proxy: sort by raw responses
                "total_pos": VendorCard.total_pos,
            }
            sort_col = sort_map.get(sort)
            if sort_col is not None:
                if order.strip().lower() == "desc":
                    query = query.order_by(None).order_by(sort_col.desc().nullslast())
                else:
                    query = query.order_by(None).order_by(sort_col.asc().nullsfirst())

        total = query.count()
        cards = query.limit(limit).offset(offset).all()
        if not cards:
            return {"vendors": [], "total": 0, "limit": limit, "offset": offset}
        # Batch fetch review stats -- single query instead of N+1
        card_ids = [c.id for c in cards]
        review_stats = {}
        if card_ids:
            for cid, avg, cnt in (
                db.query(
                    VendorReview.vendor_card_id,
                    sqlfunc.avg(VendorReview.rating),
                    sqlfunc.count(VendorReview.id),
                )
                .filter(VendorReview.vendor_card_id.in_(card_ids))
                .group_by(VendorReview.vendor_card_id)
                .all()
            ):
                review_stats[cid] = (avg, cnt)
        results = []
        for c in cards:
            stat = review_stats.get(c.id)
            avg_rating = round(float(stat[0]), 1) if stat else None
            review_count = int(stat[1]) if stat else 0
            resp_rate = None
            if c.total_outreach and c.total_outreach > 0:
                resp_rate = round((c.total_responses or 0) / c.total_outreach * 100, 1)
            results.append(
                {
                    "id": c.id,
                    "display_name": c.display_name,
                    "emails": c.emails or [],
                    "phones": c.phones or [],
                    "sighting_count": c.sighting_count or 0,
                    "vendor_score": c.vendor_score,
                    "is_new_vendor": c.is_new_vendor if c.is_new_vendor is not None else True,
                    "engagement_score": c.vendor_score,
                    "is_blacklisted": c.is_blacklisted or False,
                    "avg_rating": avg_rating,
                    "review_count": review_count,
                    "total_pos": c.total_pos or 0,
                    "response_rate": resp_rate,
                    "last_sighting_at": (c.last_activity_at or c.updated_at or c.created_at).isoformat()
                    if (c.last_activity_at or c.updated_at or c.created_at)
                    else None,
                }
            )
        return {"vendors": results, "total": total, "limit": limit, "offset": offset}

    q = q.strip().lower()
    return _fetch(q=q, tag=tag, tier=tier, sort=sort, order=order, limit=limit, offset=offset, db=db)


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
    limit = min(int(request.query_params.get("limit", "8")), 20)
    safe_q = escape_like(q)

    from sqlalchemy import String, cast

    # Primary: match on normalized_name
    vendors_by_name = (
        db.query(VendorCard)
        .filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
        .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
        .limit(limit)
        .all()
    )

    # Secondary: match on alternate_names JSON (cast to text for ILIKE)
    seen_ids = {v.id for v in vendors_by_name}
    vendors_by_alt = (
        db.query(VendorCard)
        .filter(
            cast(VendorCard.alternate_names, String).ilike(f"%{safe_q}%"),
            VendorCard.id.notin_(seen_ids) if seen_ids else True,
        )
        .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
        .limit(limit)
        .all()
    )

    companies = (
        db.query(Company.id, Company.name)
        .filter(Company.is_active, Company.name.ilike(f"%{safe_q}%"))
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
