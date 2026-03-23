"""routers/vendor_analytics.py — Vendor offer history, confirmed offers, parts summary,
and AI analysis.

Handles vendor-level analytics: offer history from MaterialVendorHistory,
confirmed buyer-entered offers, parts sighting summaries, and on-demand
AI material analysis for brand/commodity tagging.

Called by: main.py (router mount)
Depends on: models, dependencies, cache, vendor_analysis_service, sql_helpers
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_buyer, require_user
from ..models import MaterialCard, MaterialVendorHistory, Offer, User, VendorCard
from ..schemas.responses import VendorPartsSummaryResponse
from ..services.credential_service import get_credential_cached
from ..services.vendor_analysis_service import _analyze_vendor_materials
from ..utils.sql_helpers import escape_like

router = APIRouter(tags=["vendors"])


# -- Vendor Offer History ------------------------------------------------------


@router.get("/api/vendors/{card_id}/offer-history")
async def get_vendor_offer_history(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All parts this vendor has ever offered, from MaterialVendorHistory."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    q = request.query_params.get("q", "").strip().lower()
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        raise HTTPException(400, "limit and offset must be integers")

    query = (
        db.query(MaterialVendorHistory, MaterialCard)
        .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
        .filter(
            MaterialVendorHistory.vendor_name == card.normalized_name,
            MaterialCard.deleted_at.is_(None),
        )
    )
    if q:
        safe_q = escape_like(q)
        query = query.filter(MaterialCard.normalized_mpn.ilike(f"%{safe_q}%"))

    total = query.count()
    results = query.order_by(MaterialVendorHistory.last_seen.desc()).offset(offset).limit(limit).all()

    return {
        "vendor_name": card.display_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "mpn": mc.display_mpn,
                "manufacturer": mvh.last_manufacturer or mc.manufacturer or "",
                "qty": mvh.last_qty,
                "price": mvh.last_price,
                "currency": mvh.last_currency or "USD",
                "source_type": mvh.source_type,
                "times_seen": mvh.times_seen or 1,
                "first_seen": mvh.first_seen.isoformat() if mvh.first_seen else None,
                "last_seen": mvh.last_seen.isoformat() if mvh.last_seen else None,
                "material_card_id": mc.id,
            }
            for mvh, mc in results
        ],
    }


# -- Confirmed Offers (Buyer-entered quotes) -----------------------------------


@router.get("/api/vendors/{card_id}/confirmed-offers")
async def get_vendor_confirmed_offers(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirmed quotes manually entered by buyers for this vendor."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    q = request.query_params.get("q", "").strip().lower()
    try:
        limit = min(int(request.query_params.get("limit", "50")), 200)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        raise HTTPException(400, "limit and offset must be integers")

    query = db.query(Offer).filter(Offer.vendor_card_id == card_id)
    if q:
        safe_q = escape_like(q)
        query = query.filter(Offer.mpn.ilike(f"%{safe_q}%"))

    total = query.count()
    rows = query.order_by(Offer.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "vendor_name": card.display_name,
        "total": total,
        "items": [
            {
                "id": o.id,
                "mpn": o.mpn,
                "manufacturer": o.manufacturer or "",
                "qty_available": o.qty_available,
                "unit_price": float(o.unit_price) if o.unit_price is not None else None,
                "currency": o.currency or "USD",
                "lead_time": o.lead_time or "",
                "condition": o.condition or "",
                "status": o.status or "active",
                "notes": o.notes or "",
                "entered_by": o.entered_by.name if o.entered_by else "",
                "requisition_id": o.requisition_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in rows
        ],
    }


# -- Parts Sightings Summary ---------------------------------------------------


@router.get(
    "/api/vendors/{card_id}/parts-summary", response_model=VendorPartsSummaryResponse, response_model_exclude_none=True
)
async def get_vendor_parts_summary(
    card_id: int,
    q: str = Query("", description="MPN search filter"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parts this vendor has been seen with, grouped by MPN with counts and date
    ranges."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    # Check cache first
    from ..cache.decorators import cached_endpoint

    @cached_endpoint(prefix="vendor_parts_summary", ttl_hours=2, key_params=["card_id", "q", "limit", "offset"])
    def _fetch_parts(card_id, q, limit, offset, db, norm, display_name):
        return _vendor_parts_summary_query(db, norm, display_name, q, limit, offset)

    return _fetch_parts(
        card_id=card_id,
        q=q,
        limit=limit,
        offset=offset,
        db=db,
        norm=card.normalized_name,
        display_name=card.display_name,
    )


def _vendor_parts_summary_query(db, norm, display_name, q, limit, offset):
    """Execute the parts summary query (extracted for caching)."""
    q = q.strip().lower()

    # Combine sightings and material_vendor_history into a unified parts summary
    # Use parameterized filtering to avoid SQL injection via f-string interpolation
    params: dict = {"norm": norm, "off": offset, "lim": limit}
    if q:
        safe_q = escape_like(q)
        params["mpn_pattern"] = f"%{safe_q}%"
        params["has_filter"] = True
    else:
        params["mpn_pattern"] = "%"
        params["has_filter"] = False

    dialect = db.bind.dialect.name if db.bind else ""

    # PostgreSQL supports array_agg(col ORDER BY ...) for "last" value;
    # SQLite does not, so we fall back to a correlated subquery.
    if dialect == "postgresql":
        last_price_expr = "(array_agg(unit_price ORDER BY created_at DESC))[1]"
        last_qty_expr = "(array_agg(qty_available ORDER BY created_at DESC))[1]"
    else:
        last_price_expr = (
            "(SELECT s2.unit_price FROM sightings s2"
            " WHERE s2.vendor_name_normalized = sightings.vendor_name_normalized"
            " AND COALESCE(s2.mpn_matched, '') = COALESCE(sightings.mpn_matched, '')"
            " ORDER BY s2.created_at DESC LIMIT 1)"
        )
        last_qty_expr = (
            "(SELECT s2.qty_available FROM sightings s2"
            " WHERE s2.vendor_name_normalized = sightings.vendor_name_normalized"
            " AND COALESCE(s2.mpn_matched, '') = COALESCE(sightings.mpn_matched, '')"
            " ORDER BY s2.created_at DESC LIMIT 1)"
        )

    rows = db.execute(
        sqltext(f"""
        SELECT mpn, manufacturer, sighting_count, first_seen, last_seen, last_price, last_qty
        FROM (
            SELECT
                COALESCE(mpn_matched, '') as mpn,
                MAX(manufacturer) as manufacturer,
                COUNT(*) as sighting_count,
                MIN(created_at) as first_seen,
                MAX(created_at) as last_seen,
                {last_price_expr} as last_price,
                {last_qty_expr} as last_qty
            FROM sightings
            WHERE vendor_name_normalized = :norm
              AND mpn_matched IS NOT NULL AND mpn_matched != ''
            GROUP BY COALESCE(mpn_matched, '')
            UNION ALL
            SELECT
                mc.display_mpn as mpn,
                COALESCE(mvh.last_manufacturer, mc.manufacturer, '') as manufacturer,
                mvh.times_seen as sighting_count,
                mvh.first_seen,
                mvh.last_seen,
                mvh.last_price,
                mvh.last_qty
            FROM material_vendor_history mvh
            JOIN material_cards mc ON mc.id = mvh.material_card_id
            WHERE mvh.vendor_name = :norm
        ) combined
        WHERE mpn != ''
          AND (:has_filter = false OR LOWER(mpn) LIKE :mpn_pattern ESCAPE '\\')
        ORDER BY last_seen DESC NULLS LAST
        OFFSET :off LIMIT :lim
    """),
        params,
    ).fetchall()

    # Get total count
    count_params: dict = {"norm": norm, "mpn_pattern": params["mpn_pattern"], "has_filter": params["has_filter"]}
    total = (
        db.execute(
            sqltext("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT COALESCE(mpn_matched, '') as mpn FROM sightings
            WHERE vendor_name_normalized = :norm AND mpn_matched IS NOT NULL AND mpn_matched != ''
            UNION
            SELECT DISTINCT mc.display_mpn as mpn FROM material_vendor_history mvh
            JOIN material_cards mc ON mc.id = mvh.material_card_id
            WHERE mvh.vendor_name = :norm
        ) all_mpns
        WHERE mpn != ''
          AND (:has_filter = false OR LOWER(mpn) LIKE :mpn_pattern ESCAPE '\\')
    """),
            count_params,
        ).scalar()
        or 0
    )

    return {
        "vendor_name": display_name,
        "total": total,
        "items": [
            {
                "mpn": r[0],
                "manufacturer": r[1] or "",
                "sighting_count": r[2] or 1,
                "first_seen": r[3].isoformat() if r[3] else None,
                "last_seen": r[4].isoformat() if r[4] else None,
                "last_price": r[5],
                "last_qty": r[6],
            }
            for r in rows
        ],
    }


# -- AI Material Analysis ------------------------------------------------------


@router.post("/api/vendors/{card_id}/analyze-materials")
async def analyze_vendor_materials(card_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    """On-demand AI analysis of vendor's material inventory to generate brand/commodity
    tags."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        raise HTTPException(503, "AI not configured — set ANTHROPIC_API_KEY in .env")

    await _analyze_vendor_materials(card_id, db_session=db)

    # Refresh after update
    db.refresh(card)
    return {
        "ok": True,
        "brand_tags": card.brand_tags or [],
        "commodity_tags": card.commodity_tags or [],
    }
