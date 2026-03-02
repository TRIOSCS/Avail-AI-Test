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

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..cache.decorators import cached_endpoint, invalidate_prefix
from ..database import get_db
from ..dependencies import get_req_for_user, require_buyer, require_user
from ..models import (
    ActivityLog,
    ChangeLog,
    Contact,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveMatch,
    Quote,
    Requirement,
    RequirementAttachment,
    Requisition,
    RequisitionAttachment,
    Sighting,
    User,
    VendorResponse,
)
from ..rate_limit import limiter
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
    _deduplicate_sightings,
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
from ..utils.sql_helpers import escape_like
from ..vendor_utils import normalize_vendor_name
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


@router.get("/api/requisitions/counts")
async def requisition_counts(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lightweight counts for dashboard widgets — avoids the heavy list query."""
    total = db.scalar(select(sqlfunc.count(Requisition.id)))
    open_cnt = db.scalar(
        select(sqlfunc.count(Requisition.id)).where(Requisition.status.in_(["open", "active", "sourcing"]))
    )
    archive_cnt = db.scalar(select(sqlfunc.count(Requisition.id)).where(Requisition.status == "archive"))
    return {"total": total or 0, "open": open_cnt or 0, "archive": archive_cnt or 0}


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

    @cached_endpoint(prefix="req_list", ttl_hours=0.0083, key_params=["q", "status", "limit", "offset"])
    def _fetch(q, status, limit, offset, user, db):
        return _build_requisition_list(q, status, limit, offset, user, db)

    return _fetch(q=q, status=status, limit=limit, offset=offset, user=user, db=db)


def _build_requisition_list(q, status, limit, offset, user, db):
    """Build the requisitions list response (extracted for caching)."""
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
        select(sqlfunc.coalesce(sqlfunc.sum(Requirement.target_price * Requirement.target_qty), 0))
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
        safe_q = escape_like(q.strip())
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
        creators = db.query(User.id, User.name, User.email).filter(User.id.in_(creator_ids)).all()
        creator_names = {u.id: u.name or u.email.split("@")[0] for u in creators}
    return {
        "requisitions": [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "customer_site_id": r.customer_site_id,
                "company_id": (r.customer_site.company_id if r.customer_site else None),
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
                "last_searched_at": r.last_searched_at.isoformat() if r.last_searched_at else None,
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
            for _sc, _sc_color, _sc_signals in [
                _compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt)
            ]
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
    invalidate_prefix("req_list")
    return {"id": req.id, "name": req.name}


@router.put("/api/requisitions/{req_id}/archive")
async def toggle_archive(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if req.status in ("archived", "won", "lost"):
        req.status = "active"
    else:
        req.status = "archived"
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "status": req.status}


@router.put("/api/requisitions/bulk-archive")
async def bulk_archive(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Archive all active requisitions NOT created by the current user."""

    q = db.query(Requisition).filter(
        Requisition.created_by != user.id,
        Requisition.status.notin_(["archived", "won", "lost", "closed"]),
    )
    count = q.update({"status": "archived"}, synchronize_session="fetch")
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "archived_count": count}


@router.post("/api/requisitions/{req_id}/dismiss-new-offers")
async def dismiss_new_offers(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Mark offers as viewed so the flash alert stops."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    req.offers_viewed_at = sqlfunc.now()
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True}


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
    invalidate_prefix("req_list")
    return {"ok": True, "name": req.name}


# ── Requirements ─────────────────────────────────────────────────────────
@router.get("/api/requisitions/{req_id}/requirements")
async def list_requirements(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
                sqlfunc.count(sqlfunc.distinct(Sighting.vendor_name_normalized)),
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
    contact_count = (db.query(sqlfunc.count(Contact.id)).filter(Contact.requisition_id == req_id).scalar()) or 0
    last_activity_row = db.query(sqlfunc.max(Contact.created_at)).filter(Contact.requisition_id == req_id).scalar()
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
                "sale_notes": r.sale_notes or "",
            }
        )
    return results


@router.post("/api/requisitions/{req_id}/requirements")
async def add_requirements(
    req_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
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
        # Resolve material card
        from ..search_service import resolve_material_card

        mat_card = resolve_material_card(parsed.primary_mpn, db)

        r = Requirement(
            requisition_id=req_id,
            primary_mpn=parsed.primary_mpn,
            normalized_mpn=normalize_mpn_key(parsed.primary_mpn),
            material_card_id=mat_card.id if mat_card else None,
            target_qty=parsed.target_qty,
            target_price=parsed.target_price,
            substitutes=deduped_subs[:20],
        )
        db.add(r)
        created.append(r)
    db.commit()

    # NetComponents: queue parts for automated search (background, separate DB session)
    def _nc_enqueue_batch(requirement_ids: list[int]):
        from ..database import SessionLocal
        from ..services.nc_worker.queue_manager import enqueue_for_nc_search

        bg_db = SessionLocal()
        try:
            for rid in requirement_ids:
                try:
                    enqueue_for_nc_search(rid, bg_db)
                except Exception:
                    logger.debug("NC enqueue failed for requirement %s", rid, exc_info=True)
        finally:
            bg_db.close()

    # ICsource: queue parts for automated search (background, separate DB session)
    def _ics_enqueue_batch(requirement_ids: list[int]):
        from ..database import SessionLocal
        from ..services.ics_worker.queue_manager import enqueue_for_ics_search

        bg_db = SessionLocal()
        try:
            for rid in requirement_ids:
                try:
                    enqueue_for_ics_search(rid, bg_db)
                except Exception:
                    logger.debug("ICS enqueue failed for requirement %s", rid, exc_info=True)
        finally:
            bg_db.close()

    if created:
        background_tasks.add_task(_nc_enqueue_batch, [r.id for r in created])
        background_tasks.add_task(_ics_enqueue_batch, [r.id for r in created])

    # Teams: hot requirement alert for high-value items
    try:
        from ..config import settings as cfg
        from ..services.teams import send_hot_requirement_alert

        for r in created:
            price = float(r.target_price or 0)
            qty = r.target_qty or 0
            if qty * price >= cfg.teams_hot_threshold:
                customer = (
                    req.customer_site.company.name
                    if req.customer_site and req.customer_site.company
                    else (req.customer_name or "")
                )
                asyncio.create_task(
                    send_hot_requirement_alert(
                        requirement_id=r.id,
                        mpn=r.primary_mpn,
                        target_qty=qty,
                        target_price=price,
                        customer_name=customer,
                        requisition_id=req_id,
                    )
                )
    except (AttributeError, ValueError, RuntimeError):
        logger.debug("Teams hot-requirement alert failed", exc_info=True)

    # Duplicate detection: check if any of these MPNs were quoted for the same customer recently
    duplicates = []
    if req.customer_site_id and created:
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        card_ids = [r.material_card_id for r in created if r.material_card_id]
        if card_ids:
            dup_rows = (
                db.query(Requirement.primary_mpn, Requisition.id, Requisition.name)
                .join(Requisition, Requirement.requisition_id == Requisition.id)
                .filter(
                    Requirement.material_card_id.in_(card_ids),
                    Requisition.customer_site_id == req.customer_site_id,
                    Requisition.id != req_id,
                    Requisition.created_at >= cutoff,
                    Requisition.status.notin_(["archived"]),
                )
                .all()
            )
            seen = set()
            for mpn, rid, rname in dup_rows:
                key = f"{mpn}:{rid}"
                if key not in seen:
                    seen.add(key)
                    duplicates.append({"mpn": mpn, "req_id": rid, "req_name": rname})

    return {
        "created": [{"id": r.id, "primary_mpn": r.primary_mpn} for r in created],
        "duplicates": duplicates,
    }


@router.post("/api/requisitions/{req_id}/upload")
async def upload_requirements(
    req_id: int,
    background_tasks: BackgroundTasks,
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
            subs = [s.strip() for s in sub_str.replace("\n", ",").split(",") if s.strip()]
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
        condition = normalize_condition(row.get("condition") or row.get("cond") or "")
        packaging = normalize_packaging(row.get("packaging") or row.get("package") or row.get("pkg") or "")
        date_codes = (row.get("date_codes") or row.get("date_code") or row.get("dc") or "").strip() or None
        manufacturer = (row.get("manufacturer") or row.get("brand") or row.get("mfr") or "").strip() or None
        notes = (row.get("notes") or row.get("note") or "").strip() or None
        target_price_raw = row.get("target_price") or row.get("price") or ""
        target_price = normalize_price(target_price_raw)

        # Resolve material card
        from ..search_service import resolve_material_card

        mat_card = resolve_material_card(mpn, db)

        r = Requirement(
            requisition_id=req_id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=mat_card.id if mat_card else None,
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

    # NetComponents: queue uploaded requirements for automated search (background)
    def _nc_enqueue_uploaded(requisition_id: int, count: int):
        from ..database import SessionLocal
        from ..services.nc_worker.queue_manager import enqueue_for_nc_search

        bg_db = SessionLocal()
        try:
            for r_item in (
                bg_db.query(Requirement)
                .filter(
                    Requirement.requisition_id == requisition_id,
                )
                .order_by(Requirement.id.desc())
                .limit(count)
                .all()
            ):
                try:  # pragma: no cover
                    enqueue_for_nc_search(r_item.id, bg_db)
                except Exception:
                    logger.debug("NC enqueue failed for requirement %s", r_item.id, exc_info=True)
        finally:
            bg_db.close()

    if created:
        background_tasks.add_task(_nc_enqueue_uploaded, req_id, created)

    return {"created": created, "total_rows": len(rows)}


@router.delete("/api/requirements/{item_id}")
async def delete_requirement(item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
    # Snapshot old values for changelog
    _req_track_fields = [
        "primary_mpn",
        "target_qty",
        "target_price",
        "firmware",
        "date_codes",
        "hardware_codes",
        "packaging",
        "condition",
        "notes",
        "sale_notes",
    ]
    old_vals = {f: getattr(r, f) for f in _req_track_fields}
    if data.primary_mpn is not None:
        r.primary_mpn = normalize_mpn(data.primary_mpn) or data.primary_mpn.strip()
        r.normalized_mpn = normalize_mpn_key(data.primary_mpn)
        # Re-resolve material card for new MPN
        from ..search_service import resolve_material_card

        mat_card = resolve_material_card(data.primary_mpn, db)
        r.material_card_id = mat_card.id if mat_card else None
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
    if data.sale_notes is not None:
        r.sale_notes = data.sale_notes.strip()
    # Record changes
    new_vals = {f: getattr(r, f) for f in _req_track_fields}
    for f in _req_track_fields:
        old_v = str(old_vals.get(f) or "")
        new_v = str(new_vals.get(f) or "")
        if old_v != new_v:
            db.add(
                ChangeLog(
                    entity_type="requirement",
                    entity_id=item_id,
                    user_id=user.id,
                    field_name=f,
                    old_value=old_v,
                    new_value=new_v,
                )
            )
    db.commit()
    return {"ok": True}


# ── Search ───────────────────────────────────────────────────────────────


def _enqueue_ics_nc_batch(requirement_ids: list[int]):
    """Queue requirements for ICS and NC browser-based searches (background task)."""
    from ..database import SessionLocal
    from ..services.ics_worker.queue_manager import enqueue_for_ics_search
    from ..services.nc_worker.queue_manager import enqueue_for_nc_search

    bg_db = SessionLocal()
    try:
        for rid in requirement_ids:
            try:
                enqueue_for_nc_search(rid, bg_db)
            except Exception:
                logger.debug("NC enqueue failed for requirement %s", rid, exc_info=True)
            try:
                enqueue_for_ics_search(rid, bg_db)
            except Exception:
                logger.debug("ICS enqueue failed for requirement %s", rid, exc_info=True)
    finally:
        bg_db.close()


@router.post("/api/requisitions/{req_id}/search")
@limiter.limit("20/minute")
async def search_all(
    req_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
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
    reqs_to_search = [r for r in req.requirements if not requirement_ids or r.id in requirement_ids]

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

    # Queue ICS/NC browser searches for any manually searched requirements
    req_ids = [r.id for r in reqs_to_search]
    background_tasks.add_task(_enqueue_ics_nc_batch, req_ids)

    # Enrich with vendor card ratings (no contact lookup — that happens at RFQ time)
    _enrich_with_vendor_cards(results, db)

    results["source_stats"] = list(merged_source_stats.values())
    return results


@router.post("/api/requirements/{item_id}/search")
@limiter.limit("20/minute")
async def search_one(
    item_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
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
    results = {str(r.id): {"label": r.primary_mpn or f"Req #{r.id}", "sightings": sightings}}
    _enrich_with_vendor_cards(results, db)

    # Queue ICS/NC browser searches for this requirement
    background_tasks.add_task(_enqueue_ics_nc_batch, [r.id])

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
        (db.query(Sighting).filter(Sighting.requirement_id.in_(req_ids)).order_by(Sighting.score.desc()).all())
        if req_ids
        else []
    )
    sightings_by_req: dict[int, list] = {}
    for s in all_sightings:
        sightings_by_req.setdefault(s.requirement_id, []).append(s)

    # ── Cross-requisition historical offers (via material_card_id FK) ──
    req_card_map: dict[int, set[int]] = {}
    all_card_ids: set[int] = set()
    primary_card_ids: dict[int, int | None] = {}

    # Batch-collect all substitute MPN keys to resolve in one query
    all_sub_keys: set[str] = set()
    req_sub_keys: dict[int, list[str]] = {}  # req.id → list of sub_keys
    for r in req.requirements:
        primary_card_ids[r.id] = r.material_card_id
        sub_keys = []
        for sub in r.substitutes or []:
            sub_str = (sub if isinstance(sub, str) else "").strip()
            if sub_str:
                sub_key = normalize_mpn_key(sub_str)
                if sub_key:
                    sub_keys.append(sub_key)
                    all_sub_keys.add(sub_key)
        req_sub_keys[r.id] = sub_keys

    # Single batch query for all substitute material cards
    sub_card_lookup: dict[str, int] = {}
    if all_sub_keys:
        rows = (
            db.query(MaterialCard.id, MaterialCard.normalized_mpn)
            .filter(MaterialCard.normalized_mpn.in_(all_sub_keys))
            .all()
        )
        sub_card_lookup = {row.normalized_mpn: row.id for row in rows}

    # Build req_card_map using batch results
    for r in req.requirements:
        card_ids: set[int] = set()
        if r.material_card_id:
            card_ids.add(r.material_card_id)
        for sub_key in req_sub_keys.get(r.id, []):
            card_id = sub_card_lookup.get(sub_key)
            if card_id:
                card_ids.add(card_id)
        req_card_map[r.id] = card_ids
        all_card_ids |= card_ids

    hist_by_req: dict[int, list] = {}
    if all_card_ids:
        hist_query = (
            db.query(Offer)
            .filter(
                Offer.requisition_id != req_id,
                Offer.material_card_id.in_(all_card_ids),
                Offer.status.in_(["active", "won"]),
            )
            .options(joinedload(Offer.entered_by))
            .order_by(Offer.created_at.desc())
            .limit(100)
            .all()
        )
        for ho in hist_query:
            for r in req.requirements:
                if ho.material_card_id in req_card_map.get(r.id, set()):
                    if r.id not in hist_by_req:
                        hist_by_req[r.id] = []
                    is_sub = ho.material_card_id != primary_card_ids.get(r.id)
                    hist_by_req[r.id].append(
                        {
                            "id": ho.id,
                            "vendor_name": ho.vendor_name,
                            "mpn": ho.mpn,
                            "manufacturer": ho.manufacturer,
                            "qty_available": ho.qty_available,
                            "unit_price": float(ho.unit_price) if ho.unit_price else None,
                            "lead_time": ho.lead_time,
                            "condition": ho.condition,
                            "source": ho.source,
                            "status": ho.status,
                            "entered_by": ho.entered_by.name if ho.entered_by else None,
                            "created_at": ho.created_at.isoformat() if ho.created_at else None,
                            "from_requisition_id": ho.requisition_id,
                            "is_substitute": is_sub,
                        }
                    )
                    break

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
        card_ids = list(req_card_map.get(r.id, set()))
        history = _get_material_history(card_ids, fresh_vendors, db)
        for h in history:
            sighting_dicts.append(_history_to_result(h, now))

        hist_offers = hist_by_req.get(r.id, [])
        sighting_dicts = _deduplicate_sightings(sighting_dicts)
        if not sighting_dicts and not hist_offers:
            continue
        sighting_dicts.sort(key=lambda x: x.get("score", 0), reverse=True)
        results[str(r.id)] = {
            "label": label,
            "sightings": sighting_dicts,
            "historical_offers": hist_offers,
        }
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
    # Resolve parent requisition and verify user access
    req = db.query(Requisition).join(Requirement).filter(Requirement.id == s.requirement_id).first()
    if not req or not get_req_for_user(db, user, req.id):
        raise HTTPException(403, "Not authorized for this sighting")
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

    from ..file_utils import normalize_stock_row
    from ..search_service import resolve_material_card
    from ..utils.normalization import normalize_condition as norm_cond
    from ..utils.normalization import normalize_date_code, normalize_lead_time
    from ..utils.normalization import normalize_packaging as norm_pkg

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

    try:
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

            # Resolve material card
            mat_card = resolve_material_card(mpn, db)

            s = Sighting(
                requirement_id=r.id,
                material_card_id=mat_card.id if mat_card else None,
                vendor_name=vendor_name.strip(),
                vendor_name_normalized=normalize_vendor_name(vendor_name),
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
    except Exception:
        db.rollback()
        logger.exception("Stock import failed for requisition %s", req_id)
        raise HTTPException(500, "Stock import failed — no data was saved")
    return {"imported_rows": imported, "matched_sightings": matched}


# ══════════════════════════════════════════════════════════════════════
# FILE ATTACHMENTS — Requisitions & Requirements (OneDrive)
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/requisitions/{req_id}/attachments")
async def list_requisition_attachments(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return [
        {
            "id": a.id,
            "file_name": a.file_name,
            "onedrive_url": a.onedrive_url,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "uploaded_by": a.uploaded_by.name if a.uploaded_by else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in req.attachments
    ]


@router.post("/api/requisitions/{req_id}/attachments")
async def upload_requisition_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    from ..http_client import http

    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/AvailAI/Requisitions/{req_id}/{safe_name}:/content"
    resp = await http.put(
        f"https://graph.microsoft.com/v1.0{drive_path}",
        content=content,
        headers={
            "Authorization": f"Bearer {user.access_token}",
            "Content-Type": file.content_type or "application/octet-stream",
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        logger.error(f"OneDrive upload failed: {resp.status_code} {resp.text[:300]}")
        raise HTTPException(502, "Failed to upload to OneDrive")
    result = resp.json()
    att = RequisitionAttachment(
        requisition_id=req_id,
        file_name=safe_name,
        onedrive_item_id=result.get("id"),
        onedrive_url=result.get("webUrl"),
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.post("/api/requisitions/{req_id}/attachments/onedrive")
async def attach_requisition_from_onedrive(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach an existing OneDrive file to a requisition by item ID."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    body = await request.json()
    item_id = body.get("item_id")
    if not item_id:
        raise HTTPException(400, "item_id is required")
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    from ..utils.graph_client import GraphClient

    gc = GraphClient(user.access_token)
    item = await gc.get_json(f"/me/drive/items/{item_id}")
    if "error" in item:
        raise HTTPException(404, "OneDrive item not found")
    att = RequisitionAttachment(
        requisition_id=req_id,
        file_name=item.get("name", "file"),
        onedrive_item_id=item_id,
        onedrive_url=item.get("webUrl"),
        content_type=item.get("file", {}).get("mimeType"),
        size_bytes=item.get("size"),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.delete("/api/requisition-attachments/{att_id}")
async def delete_requisition_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requisition attachment (and remove from OneDrive)."""
    att = db.get(RequisitionAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if att.onedrive_item_id and user.access_token:
        try:
            from ..http_client import http

            await http.delete(
                f"https://graph.microsoft.com/v1.0/me/drive/items/{att.onedrive_item_id}",
                headers={"Authorization": f"Bearer {user.access_token}"},
                timeout=15,
            )
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to delete OneDrive item {att.onedrive_item_id}: {e}")
    db.delete(att)
    db.commit()
    return {"ok": True}


@router.get("/api/requirements/{req_id}/attachments")
async def list_requirement_attachments(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requirement."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    return [
        {
            "id": a.id,
            "file_name": a.file_name,
            "onedrive_url": a.onedrive_url,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "uploaded_by": a.uploaded_by.name if a.uploaded_by else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in requirement.attachments
    ]


@router.post("/api/requirements/{req_id}/attachments")
async def upload_requirement_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to a requirement."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    from ..http_client import http

    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/AvailAI/Requirements/{req_id}/{safe_name}:/content"
    resp = await http.put(
        f"https://graph.microsoft.com/v1.0{drive_path}",
        content=content,
        headers={
            "Authorization": f"Bearer {user.access_token}",
            "Content-Type": file.content_type or "application/octet-stream",
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        logger.error(f"OneDrive upload failed: {resp.status_code} {resp.text[:300]}")
        raise HTTPException(502, "Failed to upload to OneDrive")
    result = resp.json()
    att = RequirementAttachment(
        requirement_id=req_id,
        file_name=safe_name,
        onedrive_item_id=result.get("id"),
        onedrive_url=result.get("webUrl"),
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.delete("/api/requirement-attachments/{att_id}")
async def delete_requirement_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement attachment (and remove from OneDrive)."""
    att = db.get(RequirementAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if att.onedrive_item_id and user.access_token:
        try:
            from ..http_client import http

            await http.delete(
                f"https://graph.microsoft.com/v1.0/me/drive/items/{att.onedrive_item_id}",
                headers={"Authorization": f"Bearer {user.access_token}"},
                timeout=15,
            )
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to delete OneDrive item {att.onedrive_item_id}: {e}")
    db.delete(att)
    db.commit()
    return {"ok": True}


# ── Contacts ─────────────────────────────────────────────────────────────
