"""
requisitions.py — Requisitions, Requirements, Search & Stock Import Router

CRUD operations for requisitions and their line-item requirements.
Multi-source search triggering, sighting management, and stock list import.

Business Rules:
- Requisitions contain requirements (parent/child)
- Search queries all active connectors in parallel
- Stock import creates sightings matched to requirements by MPN
- Sighting scoring uses 6 weighted factors (see scoring.py)

Called by: main.py (router mount)
Depends on: models, search_service, file_utils, scoring, vendor_utils
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..dependencies import get_req_for_user, require_buyer, require_user
from ..rate_limit import limiter
from ..models import (
    ActivityLog,
    Contact,
    CustomerSite,
    Offer,
    ProactiveMatch,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorResponse,
)
from ..schemas.requisitions import (
    RequirementCreate,
    RequirementUpdate,
    RequisitionCreate,
    RequisitionOut,
    RequisitionUpdate,
    SearchOptions,
    SightingUnavailableIn,
)
from ..schemas.responses import RequisitionListResponse
from ..search_service import (
    _get_material_history,
    _history_to_result,
    search_requirement,
    sighting_to_dict,
)
from ..utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from .rfq import _enrich_with_vendor_cards

router = APIRouter(tags=["requisitions"])


def _compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt):
    """Lightweight sourcing score for list views."""
    from ..services.sourcing_score import compute_requisition_score_fast

    return compute_requisition_score_fast(
        req_count=req_cnt or 0,
        sourced_count=sourced_cnt or 0,
        rfq_sent_count=rfq_sent or 0,
        reply_count=reply_cnt or 0,
        offer_count=offer_cnt or 0,
        call_count=call_cnt or 0,
        email_count=email_act_cnt or 0,
    )


@router.get("/api/requisitions", response_model=RequisitionListResponse, response_model_exclude_none=True)
async def list_requisitions(
    q: str = "",
    status: str = "",
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List requisitions with filtering, search, and sourcing scores."""
    # Single query with subquery counts — avoids N+1 lazy loads
    req_count_sq = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("requirement_count")
    )
    contact_count_sq = (
        select(sqlfunc.count(Contact.id))
        .where(Contact.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("contact_count")
    )
    rfq_sent_count_sq = (
        select(sqlfunc.count(Contact.id))
        .where(
            Contact.requisition_id == Requisition.id,
            Contact.status == "sent",
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("rfq_sent_count")
    )
    reply_count_sq = (
        select(sqlfunc.count(VendorResponse.id))
        .where(VendorResponse.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("reply_count")
    )
    latest_reply_sq = (
        select(sqlfunc.max(VendorResponse.received_at))
        .where(VendorResponse.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("latest_reply_at")
    )
    # Detect unseen offers: latest offer created_at > offers_viewed_at (or viewed_at is NULL and offers exist)
    from sqlalchemy import and_, case, literal, or_

    latest_offer_sq = (
        select(sqlfunc.max(Offer.created_at))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
    )
    has_new_offers_sq = case(
        (
            and_(
                latest_offer_sq.isnot(None),
                or_(
                    Requisition.offers_viewed_at.is_(None),
                    latest_offer_sq > Requisition.offers_viewed_at,
                ),
            ),
            literal(True),
        ),
        else_=literal(False),
    ).label("has_new_offers")

    latest_offer_at_sq = latest_offer_sq.label("latest_offer_at")
    # Count requirements that have at least one sighting (for progress indicator)
    sourced_count_sq = (
        select(sqlfunc.count(sqlfunc.distinct(Requirement.id)))
        .where(
            Requirement.requisition_id == Requisition.id,
            select(sqlfunc.count(Sighting.id))
            .where(Sighting.requirement_id == Requirement.id)
            .correlate(Requirement)
            .scalar_subquery()
            > 0,
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("sourced_count")
    )
    # Count vendor responses needing human review (needs_action=True)
    needs_review_sq = (
        select(sqlfunc.count(VendorResponse.id))
        .where(
            VendorResponse.requisition_id == Requisition.id,
            VendorResponse.needs_action.is_(True),
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("needs_review_count")
    )
    # Sum of target_price * target_qty across requirements (for high-value filter)
    total_target_value_sq = (
        select(
            sqlfunc.coalesce(
                sqlfunc.sum(Requirement.target_price * Requirement.target_qty), 0
            )
        )
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("total_target_value")
    )
    # ── New card-summary subqueries ─────────────────────────────────────
    # Best quote status (priority: won > lost > sent > revised > draft)
    _quote_priority = case(
        (Quote.status == "won", literal(1)),
        (Quote.status == "lost", literal(2)),
        (Quote.status == "sent", literal(3)),
        (Quote.status == "revised", literal(4)),
        else_=literal(5),
    )
    quote_status_sq = (
        select(Quote.status)
        .where(Quote.requisition_id == Requisition.id)
        .correlate(Requisition)
        .order_by(_quote_priority)
        .limit(1)
        .scalar_subquery()
        .label("quote_status")
    )
    # Quote sent date — most recent
    quote_sent_at_sq = (
        select(sqlfunc.max(Quote.sent_at))
        .where(Quote.requisition_id == Requisition.id, Quote.sent_at.isnot(None))
        .correlate(Requisition)
        .scalar_subquery()
        .label("quote_sent_at")
    )
    # Quote total — subtotal from the best-priority quote
    quote_total_sq = (
        select(Quote.subtotal)
        .where(Quote.requisition_id == Requisition.id)
        .correlate(Requisition)
        .order_by(_quote_priority)
        .limit(1)
        .scalar_subquery()
        .label("quote_total")
    )
    # Won revenue — max won_revenue from won quotes
    quote_won_value_sq = (
        select(sqlfunc.max(Quote.won_revenue))
        .where(Quote.requisition_id == Requisition.id, Quote.status == "won")
        .correlate(Requisition)
        .scalar_subquery()
        .label("quote_won_value")
    )
    # Offer count
    offer_count_sq = (
        select(sqlfunc.count(Offer.id))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("offer_count")
    )
    # Best offer price (lowest positive unit_price)
    best_offer_price_sq = (
        select(sqlfunc.min(Offer.unit_price))
        .where(Offer.requisition_id == Requisition.id, Offer.unit_price > 0)
        .correlate(Requisition)
        .scalar_subquery()
        .label("best_offer_price")
    )
    # Awaiting reply count — contacts with status in ('sent', 'opened')
    awaiting_reply_sq = (
        select(sqlfunc.count(Contact.id))
        .where(
            Contact.requisition_id == Requisition.id,
            Contact.status.in_(["sent", "opened"]),
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("awaiting_reply_count")
    )
    # Proactive match count — non-dismissed matches
    proactive_match_count_sq = (
        select(sqlfunc.count(ProactiveMatch.id))
        .where(
            ProactiveMatch.requisition_id == Requisition.id,
            ProactiveMatch.status != "dismissed",
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("proactive_match_count")
    )
    # Sourcing activity score signals: phone calls and emails on this requisition
    call_count_sq = (
        select(sqlfunc.count(ActivityLog.id))
        .where(
            ActivityLog.requisition_id == Requisition.id,
            ActivityLog.channel == "phone",
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("call_count")
    )
    email_activity_count_sq = (
        select(sqlfunc.count(ActivityLog.id))
        .where(
            ActivityLog.requisition_id == Requisition.id,
            ActivityLog.channel == "email",
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("email_activity_count")
    )

    query = db.query(
        Requisition,
        req_count_sq,
        contact_count_sq,
        reply_count_sq,
        latest_reply_sq,
        has_new_offers_sq,
        latest_offer_at_sq,
        sourced_count_sq,
        rfq_sent_count_sq,
        needs_review_sq,
        total_target_value_sq,
        quote_status_sq,
        quote_sent_at_sq,
        quote_total_sq,
        quote_won_value_sq,
        offer_count_sq,
        best_offer_price_sq,
        awaiting_reply_sq,
        proactive_match_count_sq,
        call_count_sq,
        email_activity_count_sq,
    ).options(
        joinedload(Requisition.customer_site).joinedload(CustomerSite.company),
    )
    # Sales sees own reqs only; all other roles see everything
    if user.role == "sales":
        query = query.filter(Requisition.created_by == user.id)

    if q.strip():
        safe_q = q.strip().replace("%", r"\%").replace("_", r"\_")
        from sqlalchemy import exists, or_
        # Search by req name, customer name, primary MPN, or substitutes
        # Split into separate EXISTS so PostgreSQL can use trigram indexes
        mpn_match = exists(
            select(Requirement.id).where(
                Requirement.requisition_id == Requisition.id,
                Requirement.primary_mpn.ilike(f"%{safe_q}%"),
            )
        )
        subs_match = exists(
            select(Requirement.id).where(
                Requirement.requisition_id == Requisition.id,
                Requirement.substitutes_text.ilike(f"%{safe_q}%"),
            )
        )
        query = query.filter(
            or_(
                Requisition.name.ilike(f"%{safe_q}%"),
                Requisition.customer_name.ilike(f"%{safe_q}%"),
                mpn_match,
                subs_match,
            )
        )
    elif status == "archive":
        query = query.filter(Requisition.status.in_(["archived", "won", "lost", "closed"]))
    else:
        query = query.filter(Requisition.status.notin_(["archived", "won", "lost", "closed"]))

    # Skip expensive COUNT for search queries — use result length instead
    rows = query.order_by(Requisition.created_at.desc()).offset(offset).limit(limit).all()
    total = (offset + len(rows)) if q.strip() else query.count()
    # Pre-load creator names (all roles see all reqs now)
    creator_names = {}
    creator_ids = {r.created_by for r, *_ in rows if r.created_by}
    if creator_ids:
        creators = (
            db.query(User.id, User.name, User.email)
            .filter(User.id.in_(creator_ids))
            .all()
        )
        creator_names = {u.id: u.name or u.email.split("@")[0] for u in creators}
    return {
        "requisitions": [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "customer_site_id": r.customer_site_id,
                "company_id": (
                    r.customer_site.company_id
                    if r.customer_site
                    else None
                ),
                "customer_display": (
                    f"{r.customer_site.company.name} — {r.customer_site.site_name}"
                    if r.customer_site and r.customer_site.company
                    else r.customer_name or ""
                ),
                "requirement_count": req_cnt,
                "contact_count": con_cnt,
                "reply_count": reply_cnt or 0,
                "latest_reply_at": latest_reply.isoformat() if latest_reply else None,
                "has_new_offers": bool(has_new),
                "latest_offer_at": latest_offer.isoformat() if latest_offer else None,
                "created_by": r.created_by,
                "created_by_name": creator_names.get(r.created_by, ""),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_searched_at": r.last_searched_at.isoformat()
                if r.last_searched_at
                else None,
                "sourced_count": sourced_cnt or 0,
                "rfq_sent_count": rfq_sent or 0,
                "cloned_from_id": r.cloned_from_id,
                "deadline": r.deadline,
                "needs_review_count": needs_rev or 0,
                "total_target_value": float(ttv or 0),
                "quote_status": q_status,
                "quote_sent_at": q_sent.isoformat() if q_sent else None,
                "quote_total": float(q_total) if q_total else None,
                "quote_won_value": float(q_won) if q_won else None,
                "offer_count": offer_cnt or 0,
                "best_offer_price": float(best_price) if best_price else None,
                "awaiting_reply_count": await_cnt or 0,
                "proactive_match_count": pm_cnt or 0,
                "sourcing_score": _sc,
                "sourcing_color": _sc_color,
                "sourcing_signals": _sc_signals,
            }
            for r, req_cnt, con_cnt, reply_cnt, latest_reply, has_new, latest_offer, sourced_cnt, rfq_sent, needs_rev, ttv, q_status, q_sent, q_total, q_won, offer_cnt, best_price, await_cnt, pm_cnt, call_cnt, email_act_cnt in rows
            for _sc, _sc_color, _sc_signals in [_compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt)]
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/requisitions/{req_id}/sourcing-score")
async def requisition_sourcing_score(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get detailed per-requirement sourcing scores for a requisition."""
    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    from ..services.sourcing_score import compute_requisition_scores

    return compute_requisition_scores(req_id, db)


@router.post("/api/requisitions", response_model=RequisitionOut)
async def create_requisition(
    body: RequisitionCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = Requisition(
        name=body.name,
        customer_site_id=body.customer_site_id,
        customer_name=body.customer_name,
        deadline=body.deadline,
        created_by=user.id,
        status="draft",
    )
    db.add(req)
    db.commit()
    return {"id": req.id, "name": req.name}


@router.put("/api/requisitions/{req_id}/archive")
async def toggle_archive(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if req.status in ("archived", "won", "lost"):
        req.status = "active"
    else:
        req.status = "archived"
    db.commit()
    return {"ok": True, "status": req.status}


@router.put("/api/requisitions/bulk-archive")
async def bulk_archive(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Archive all active requisitions NOT created by the current user."""
    from ..dependencies import user_reqs_query

    q = db.query(Requisition).filter(
        Requisition.created_by != user.id,
        Requisition.status.notin_(["archived", "won", "lost", "closed"]),
    )
    count = q.update({"status": "archived"}, synchronize_session="fetch")
    db.commit()
    return {"ok": True, "archived_count": count}


@router.post("/api/requisitions/{req_id}/clone")
async def clone_requisition(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Clone an archived (or any) requisition into a new draft with all its requirements."""
    src = get_req_for_user(db, user, req_id)
    if not src:
        raise HTTPException(404, "Requisition not found")
    clone = Requisition(
        name=f"{src.name} (copy)",
        customer_site_id=src.customer_site_id,
        customer_name=src.customer_name,
        deadline=src.deadline,
        created_by=user.id,
        status="draft",
        cloned_from_id=src.id,
    )
    db.add(clone)
    db.flush()  # get clone.id
    for r in src.requirements:
        # Re-normalize cloned data and dedup substitutes
        cloned_mpn = normalize_mpn(r.primary_mpn) or r.primary_mpn
        seen_keys = {normalize_mpn_key(cloned_mpn)}
        deduped_subs = []
        for s in (r.substitutes or []):
            ns = normalize_mpn(s) or s
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)
        db.add(Requirement(
            requisition_id=clone.id,
            primary_mpn=cloned_mpn,
            normalized_mpn=normalize_mpn_key(cloned_mpn),
            target_qty=r.target_qty,
            target_price=r.target_price,
            substitutes=deduped_subs[:20],
            firmware=r.firmware,
            date_codes=r.date_codes,
            hardware_codes=r.hardware_codes,
            packaging=normalize_packaging(r.packaging) or r.packaging,
            condition=normalize_condition(r.condition) or r.condition,
            notes=r.notes,
        ))
    db.commit()
    return {"ok": True, "id": clone.id, "name": clone.name}


@router.put("/api/requisitions/{req_id}")
async def update_requisition(
    req_id: int,
    body: RequisitionUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if body.name is not None:
        req.name = body.name.strip() or req.name
    if body.customer_site_id is not None:
        req.customer_site_id = body.customer_site_id
    if body.deadline is not None:
        req.deadline = body.deadline or None
    db.commit()
    return {"ok": True, "name": req.name}


# ── Requirements ─────────────────────────────────────────────────────────
@router.get("/api/requisitions/{req_id}/requirements")
async def list_requirements(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """List requirements for a requisition with sighting counts."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    # Single query: get vendor counts per requirement via SQL (avoids loading all sightings)
    vendor_counts = {}
    offer_counts = {}
    if req.requirements:
        req_ids = [r.id for r in req.requirements]
        rows = (
            db.query(
                Sighting.requirement_id,
                sqlfunc.count(
                    sqlfunc.distinct(sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name)))
                ),
            )
            .filter(
                Sighting.requirement_id.in_(req_ids),
                Sighting.vendor_name.isnot(None),
            )
            .group_by(Sighting.requirement_id)
            .all()
        )
        for rid, cnt in rows:
            vendor_counts[rid] = cnt

        # Offer counts per requirement (for status badges)
        offer_rows = (
            db.query(Offer.requirement_id, sqlfunc.count(Offer.id))
            .filter(
                Offer.requirement_id.in_(req_ids),
                Offer.status.in_(["active", "won"]),
            )
            .group_by(Offer.requirement_id)
            .all()
        )
        for rid, cnt in offer_rows:
            offer_counts[rid] = cnt

    # Requisition-level contact count and last activity timestamp
    contact_count = (
        db.query(sqlfunc.count(Contact.id))
        .filter(Contact.requisition_id == req_id)
        .scalar()
    ) or 0
    last_activity_row = (
        db.query(sqlfunc.max(Contact.created_at))
        .filter(Contact.requisition_id == req_id)
        .scalar()
    )
    hours_since = None
    if last_activity_row:
        from datetime import datetime, timezone

        delta = datetime.now(timezone.utc) - last_activity_row.replace(tzinfo=timezone.utc)
        hours_since = delta.total_seconds() / 3600

    results = []
    for r in req.requirements:
        results.append(
            {
                "id": r.id,
                "primary_mpn": r.primary_mpn,
                "target_qty": r.target_qty,
                "target_price": float(r.target_price) if r.target_price else None,
                "substitutes": r.substitutes or [],
                "sighting_count": vendor_counts.get(r.id, 0),
                "offer_count": offer_counts.get(r.id, 0),
                "contact_count": contact_count,
                "hours_since_activity": round(hours_since, 1) if hours_since is not None else None,
                "brand": r.brand or "",
                "firmware": r.firmware or "",
                "date_codes": r.date_codes or "",
                "hardware_codes": r.hardware_codes or "",
                "packaging": r.packaging or "",
                "condition": r.condition or "",
                "notes": r.notes or "",
            }
        )
    return results


@router.post("/api/requisitions/{req_id}/requirements")
async def add_requirements(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    raw = await request.json()
    items = raw if isinstance(raw, list) else [raw]
    created = []
    for item in items:
        try:
            parsed = RequirementCreate.model_validate(item)
        except (ValueError, TypeError):
            continue  # skip invalid items (matches prior behaviour of skipping blank mpn)
        # Dedup substitutes by canonical key (schema already normalizes display form)
        seen_keys = {normalize_mpn_key(parsed.primary_mpn)}
        deduped_subs = []
        for s in parsed.substitutes:
            key = normalize_mpn_key(s)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(s)
        r = Requirement(
            requisition_id=req_id,
            primary_mpn=parsed.primary_mpn,
            normalized_mpn=normalize_mpn_key(parsed.primary_mpn),
            target_qty=parsed.target_qty,
            target_price=parsed.target_price,
            substitutes=deduped_subs[:20],
        )
        db.add(r)
        created.append(r)
    db.commit()

    # Teams: hot requirement alert for high-value items
    try:
        from ..config import settings as cfg
        from ..services.teams import send_hot_requirement_alert
        for r in created:
            price = float(r.target_price or 0)
            qty = r.target_qty or 0
            if qty * price >= cfg.teams_hot_threshold:
                customer = req.customer_site.company.name if req.customer_site and req.customer_site.company else (req.customer_name or "")
                asyncio.create_task(send_hot_requirement_alert(
                    requirement_id=r.id,
                    mpn=r.primary_mpn,
                    target_qty=qty,
                    target_price=price,
                    customer_name=customer,
                    requisition_id=req_id,
                ))
    except (AttributeError, ValueError, RuntimeError):
        logger.debug("Teams hot-requirement alert failed", exc_info=True)

    return [{"id": r.id, "primary_mpn": r.primary_mpn} for r in created]


@router.post("/api/requisitions/{req_id}/upload")
async def upload_requirements(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = (file.filename or "").lower()
    try:
        from ..file_utils import parse_tabular_file

        rows = parse_tabular_file(content, fname)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Could not parse file: {str(e)[:200]}")

    created = 0
    for row in rows:
        raw_mpn = (
            row.get("primary_mpn")
            or row.get("mpn")
            or row.get("part_number")
            or row.get("part")
            or row.get("pn")
            or row.get("oem_pn")
            or row.get("oem")
            or row.get("sku")
            or ""
        )
        mpn = normalize_mpn(raw_mpn)
        if not mpn:
            continue
        qty_raw = row.get("target_qty") or row.get("qty") or row.get("quantity") or "1"
        qty = normalize_quantity(qty_raw) or 1
        subs = []
        sub_str = row.get("substitutes") or row.get("subs") or ""
        if sub_str:
            subs = [
                s.strip() for s in sub_str.replace("\n", ",").split(",") if s.strip()
            ]
        for i in range(1, 21):
            s = row.get(f"sub_{i}") or row.get(f"sub{i}") or ""
            if s:
                subs.append(s)
        # Normalize each substitute and dedup by canonical key
        seen_keys = {normalize_mpn_key(mpn)}
        deduped_subs = []
        for s in subs:
            ns = normalize_mpn(s)
            if not ns:
                continue
            key = normalize_mpn_key(ns)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)

        # Parse optional columns
        condition = normalize_condition(
            row.get("condition") or row.get("cond") or ""
        )
        packaging = normalize_packaging(
            row.get("packaging") or row.get("package") or row.get("pkg") or ""
        )
        date_codes = (
            row.get("date_codes") or row.get("date_code") or row.get("dc") or ""
        ).strip() or None
        manufacturer = (
            row.get("manufacturer") or row.get("brand") or row.get("mfr") or ""
        ).strip() or None
        notes = (row.get("notes") or row.get("note") or "").strip() or None
        target_price_raw = row.get("target_price") or row.get("price") or ""
        target_price = normalize_price(target_price_raw)

        r = Requirement(
            requisition_id=req_id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            target_qty=qty,
            target_price=target_price,
            substitutes=deduped_subs[:20],
            condition=condition,
            packaging=packaging,
            date_codes=date_codes,
            brand=manufacturer,
            notes=notes,
        )
        db.add(r)
        created += 1
    db.commit()
    return {"created": created, "total_rows": len(rows)}


@router.delete("/api/requirements/{item_id}")
async def delete_requirement(
    item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Not authorized for this requisition")
    db.delete(r)
    db.commit()
    return {"ok": True}


@router.put("/api/requirements/{item_id}")
async def update_requirement(
    item_id: int,
    data: RequirementUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Not authorized for this requisition")
    if data.primary_mpn is not None:
        r.primary_mpn = normalize_mpn(data.primary_mpn) or data.primary_mpn.strip()
        r.normalized_mpn = normalize_mpn_key(data.primary_mpn)
    if data.target_qty is not None:
        r.target_qty = data.target_qty
    if data.substitutes is not None:
        # Normalize each substitute and dedup by canonical key
        seen_keys = {normalize_mpn_key(r.primary_mpn)}
        deduped = []
        for s in data.substitutes:
            ns = normalize_mpn(s) or s.strip()
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append(ns)
        r.substitutes = deduped[:20]
    if data.target_price is not None:
        r.target_price = data.target_price
    if data.firmware is not None:
        r.firmware = data.firmware.strip()
    if data.date_codes is not None:
        r.date_codes = data.date_codes.strip()
    if data.hardware_codes is not None:
        r.hardware_codes = data.hardware_codes.strip()
    if data.packaging is not None:
        r.packaging = normalize_packaging(data.packaging) or data.packaging.strip()
    if data.condition is not None:
        r.condition = normalize_condition(data.condition) or data.condition.strip()
    if data.notes is not None:
        r.notes = data.notes.strip()
    db.commit()
    return {"ok": True}


# ── Search ───────────────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/search")
@limiter.limit("20/minute")
async def search_all(
    req_id: int,
    request: Request,
    body: SearchOptions | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    # Optional: only search specific requirements
    requirement_ids = body.requirement_ids if body else None

    # Filter requirements to search
    reqs_to_search = [
        r for r in req.requirements
        if not requirement_ids or r.id in requirement_ids
    ]

    # Search all requirements in parallel
    search_tasks = [search_requirement(r, db) for r in reqs_to_search]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    results = {}
    merged_source_stats: dict[str, dict] = {}
    for r, search_result in zip(reqs_to_search, search_results):
        if isinstance(search_result, Exception):
            logger.error(f"Search failed for requirement {r.id}: {search_result}")
            sightings = []
            req_stats = []
        else:
            sightings = search_result["sightings"]
            req_stats = search_result["source_stats"]
        label = r.primary_mpn or f"Req #{r.id}"
        results[str(r.id)] = {"label": label, "sightings": sightings}
        # Merge source_stats across requirements (same connectors run for each)
        for stat in req_stats:
            name = stat["source"]
            if name not in merged_source_stats:
                merged_source_stats[name] = dict(stat)
            else:
                existing = merged_source_stats[name]
                existing["results"] += stat["results"]
                existing["ms"] = max(existing["ms"], stat["ms"])
                if stat["error"] and not existing["error"]:
                    existing["error"] = stat["error"]
                    existing["status"] = stat["status"]

    # Stamp last searched time (resets 30-day auto-archive clock)
    req.last_searched_at = datetime.now(timezone.utc)
    # Transition draft→active on first search; reactivate if archived
    if req.status in ("draft", "archived"):
        req.status = "active"
    db.commit()

    # Enrich with vendor card ratings (no contact lookup — that happens at RFQ time)
    _enrich_with_vendor_cards(results, db)

    results["source_stats"] = list(merged_source_stats.values())
    return results


@router.post("/api/requirements/{item_id}/search")
@limiter.limit("20/minute")
async def search_one(
    item_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    # Verify the user has access to the parent requisition
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Access denied")
    search_result = await search_requirement(r, db)
    sightings = search_result["sightings"]
    source_stats = search_result["source_stats"]
    # Wrap in same structure as search_all so enrichment works
    results = {
        str(r.id): {"label": r.primary_mpn or f"Req #{r.id}", "sightings": sightings}
    }
    _enrich_with_vendor_cards(results, db)

    return {"sightings": results[str(r.id)]["sightings"], "source_stats": source_stats}


# ── Saved sightings (no re-search) ──────────────────────────────────────
@router.get("/api/requisitions/{req_id}/sightings")
async def get_saved_sightings(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return previously saved sightings from DB without triggering a new search."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    now = datetime.now(timezone.utc)
    results: dict = {}
    # Batch-fetch all sightings for this requisition's requirements in one query
    req_ids = [r.id for r in req.requirements]
    all_sightings = (
        db.query(Sighting)
        .filter(Sighting.requirement_id.in_(req_ids))
        .order_by(Sighting.score.desc())
        .all()
    ) if req_ids else []
    sightings_by_req: dict[int, list] = {}
    for s in all_sightings:
        sightings_by_req.setdefault(s.requirement_id, []).append(s)

    for r in req.requirements:
        rows = sightings_by_req.get(r.id, [])
        label = r.primary_mpn or f"Req #{r.id}"
        sighting_dicts = []
        for s in rows:
            d = sighting_to_dict(s)
            d["is_historical"] = False
            d["is_material_history"] = False
            sighting_dicts.append(d)

        # Append material history (vendors seen before but not in fresh results)
        fresh_vendors = {s.vendor_name.lower() for s in rows}
        pns = [r.primary_mpn] + (r.substitutes or [])
        pns = [p for p in pns if p]
        history = _get_material_history(pns, fresh_vendors, db)
        for h in history:
            sighting_dicts.append(_history_to_result(h, now))

        if not sighting_dicts:
            continue
        sighting_dicts.sort(key=lambda x: x.get("score", 0), reverse=True)
        results[str(r.id)] = {"label": label, "sightings": sighting_dicts}
    _enrich_with_vendor_cards(results, db)
    return results


# ── Mark sighting as unavailable ─────────────────────────────────────────
@router.put("/api/sightings/{sighting_id}/unavailable")
async def mark_unavailable(
    sighting_id: int,
    data: SightingUnavailableIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Sighting, sighting_id)
    if not s:
        raise HTTPException(404, "Sighting not found")
    # Verify sighting belongs to a valid requisition
    req_check = (
        db.query(Requisition)
        .join(Requirement)
        .filter(
            Requirement.id == s.requirement_id,
        )
    )
    if not req_check.first():
        raise HTTPException(403, "Not your sighting")
    s.is_unavailable = data.unavailable
    db.commit()
    return {"ok": True, "is_unavailable": s.is_unavailable}


# ── Vendor Stock List Import ─────────────────────────────────────────────
@router.post("/api/requisitions/{req_id}/import-stock")
async def import_stock_list(
    req_id: int,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Import a vendor stock list CSV/Excel as sightings for matching requirements."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    file = form.get("file")
    vendor_name = form.get("vendor_name", "Manual Import")
    if not file:
        raise HTTPException(400, "No file uploaded")

    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = file.filename.lower()

    # Parse rows using shared utility
    from ..file_utils import parse_tabular_file

    rows = parse_tabular_file(content, fname)

    # Build a set of MPNs we're looking for in this requisition (keyed by canonical form)
    req_mpns = {}
    for r in req.requirements:
        all_mpns = [r.primary_mpn] if r.primary_mpn else []
        for sub in r.substitutes or []:
            if sub and sub.strip():
                all_mpns.append(sub.strip())
        for m in all_mpns:
            req_mpns[normalize_mpn_key(m)] = r

    matched = 0
    imported = 0

    from ..file_utils import normalize_stock_row
    from ..utils.normalization import normalize_condition as norm_cond
    from ..utils.normalization import normalize_date_code, normalize_lead_time
    from ..utils.normalization import normalize_packaging as norm_pkg

    for row in rows:
        parsed = normalize_stock_row(row)
        if not parsed:
            continue
        mpn = parsed["mpn"]
        imported += 1

        # Check if this MPN matches any requirement
        r = req_mpns.get(normalize_mpn_key(mpn))
        if not r:
            continue

        display_mpn = normalize_mpn(mpn) or mpn

        s = Sighting(
            requirement_id=r.id,
            vendor_name=vendor_name.strip(),
            mpn_matched=display_mpn,
            manufacturer=parsed.get("manufacturer"),
            qty_available=parsed.get("qty"),
            unit_price=parsed.get("price"),
            currency=parsed.get("currency", "USD"),
            condition=norm_cond(parsed.get("condition")),
            packaging=norm_pkg(parsed.get("packaging")),
            date_code=normalize_date_code(parsed.get("date_code")),
            lead_time_days=normalize_lead_time(parsed.get("lead_time")),
            source_type="stock_list",
            confidence=70,
            raw_data=row,
            created_at=datetime.now(timezone.utc),
        )
        s.score = 50  # Neutral score for manual imports
        db.add(s)
        matched += 1

    db.commit()
    return {"imported_rows": imported, "matched_sightings": matched}


# ── Contacts ─────────────────────────────────────────────────────────────
