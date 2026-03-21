"""Requisition CRUD — list, create, update, archive, outcome, counts, sourcing-score.

Business Rules:
- Requisitions contain requirements (parent/child)
- Sales users only see their own requisitions; all other roles see everything
- Archiving/outcomes use status transitions: draft → active → archived/won/lost
- Sourcing score computed from 7 weighted factors

Called by: requisitions.__init__ (sub-router)
Depends on: models, schemas, cache, dependencies, sourcing_score service
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, exists, literal, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...constants import RequisitionStatus
from ...database import get_db
from ...dependencies import get_req_for_user, require_admin, require_user
from ...models import (
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
from ...schemas.requisitions import (
    BatchArchiveByIds,
    BatchAssign,
    RequisitionCreate,
    RequisitionOut,
    RequisitionUpdate,
)
from ...schemas.responses import RequisitionListResponse
from ...utils.sql_helpers import escape_like

router = APIRouter(tags=["requisitions"])

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt):
    """Lightweight sourcing score for list views."""
    from ...services.sourcing_score import compute_requisition_score_fast

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
        select(sqlfunc.count(Requisition.id)).where(Requisition.status.in_(["open", "active", "sourcing", "draft"]))
    )
    archive_cnt = db.scalar(
        select(sqlfunc.count(Requisition.id)).where(Requisition.status.in_(["archived", "won", "lost", "closed"]))
    )
    return {"total": total or 0, "open": open_cnt or 0, "archive": archive_cnt or 0}


@router.get("/api/requisitions", response_model=RequisitionListResponse, response_model_exclude_none=True)
async def list_requisitions(
    q: str = "",
    status: str = "",
    sort: str = Query("created_at", description="Column to sort by"),
    order: str = Query("desc", description="Sort direction: asc or desc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List requisitions with filtering, search, and sourcing scores."""
    from ...cache.decorators import cached_endpoint

    @cached_endpoint(
        prefix="req_list", ttl_hours=0.0083, key_params=["q", "status", "sort", "order", "limit", "offset"]
    )
    def _fetch(q, status, sort, order, limit, offset, user, db):
        return _build_requisition_list(q, status, sort, order, limit, offset, user, db)

    return _fetch(q=q, status=status, sort=sort, order=order, limit=limit, offset=offset, user=user, db=db)


def _build_requisition_list(q, status, sort, order, limit, offset, user, db):
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
    latest_rfq_sent_sq = (
        select(sqlfunc.max(Contact.created_at))
        .where(Contact.requisition_id == Requisition.id, Contact.status == "sent")
        .correlate(Requisition)
        .scalar_subquery()
        .label("latest_rfq_sent_at")
    )
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
    total_target_value_sq = (
        select(sqlfunc.coalesce(sqlfunc.sum(Requirement.target_price * Requirement.target_qty), 0))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("total_target_value")
    )
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
    quote_sent_at_sq = (
        select(sqlfunc.max(Quote.sent_at))
        .where(Quote.requisition_id == Requisition.id, Quote.sent_at.isnot(None))
        .correlate(Requisition)
        .scalar_subquery()
        .label("quote_sent_at")
    )
    quote_total_sq = (
        select(Quote.subtotal)
        .where(Quote.requisition_id == Requisition.id)
        .correlate(Requisition)
        .order_by(_quote_priority)
        .limit(1)
        .scalar_subquery()
        .label("quote_total")
    )
    quote_won_value_sq = (
        select(sqlfunc.max(Quote.won_revenue))
        .where(Quote.requisition_id == Requisition.id, Quote.status == "won")
        .correlate(Requisition)
        .scalar_subquery()
        .label("quote_won_value")
    )
    offer_count_sq = (
        select(sqlfunc.count(Offer.id))
        .where(
            Offer.requisition_id == Requisition.id,
            Offer.status.notin_(["rejected", "deleted", "expired"]),
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("offer_count")
    )
    best_offer_price_sq = (
        select(sqlfunc.min(Offer.unit_price))
        .where(Offer.requisition_id == Requisition.id, Offer.unit_price > 0)
        .correlate(Requisition)
        .scalar_subquery()
        .label("best_offer_price")
    )
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
        latest_rfq_sent_sq,
    ).options(
        joinedload(Requisition.customer_site).joinedload(CustomerSite.company),
    )
    # Sales sees own reqs only; all other roles see everything
    if user.role == "sales":
        query = query.filter(Requisition.created_by == user.id)

    if q.strip():
        safe_q = escape_like(q.strip())
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
    elif status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            query = query.filter(Requisition.status == statuses[0])
        else:
            query = query.filter(Requisition.status.in_(statuses))
    else:
        query = query.filter(Requisition.status.notin_(["archived", "won", "lost", "closed"]))

    # Resolve sort column — whitelist to prevent SQL injection
    allowed_sorts = {
        "created_at": Requisition.created_at,
        "name": Requisition.name,
        "status": Requisition.status,
        "customer_name": Requisition.customer_name,
        "deadline": Requisition.deadline,
        "updated_at": Requisition.updated_at,
    }
    sort_col = allowed_sorts.get(sort, Requisition.created_at)
    sort_expr = sort_col.asc() if order.lower() == "asc" else sort_col.desc()

    rows = query.order_by(sort_expr).offset(offset).limit(limit).all()
    total = (offset + len(rows)) if q.strip() else query.count()
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
                "latest_rfq_sent_at": latest_rfq_sent.isoformat() if latest_rfq_sent else None,
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
                "claimed_by_id": r.claimed_by_id,
                "urgency": r.urgency or "normal",
                "opportunity_value": float(r.opportunity_value) if r.opportunity_value else None,
                "sourcing_score": _sc,
                "sourcing_color": _sc_color,
                "sourcing_signals": _sc_signals,
            }
            for r, req_cnt, con_cnt, reply_cnt, latest_reply, has_new, latest_offer, sourced_cnt, rfq_sent, needs_rev, ttv, q_status, q_sent, q_total, q_won, offer_cnt, best_price, await_cnt, pm_cnt, call_cnt, email_act_cnt, latest_rfq_sent in rows
            for _sc, _sc_color, _sc_signals in [
                _compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt)
            ]
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/requisitions/{req_id}")
async def get_requisition(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get a single requisition by ID."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return {
        "id": req.id,
        "name": req.name,
        "status": req.status,
        "customer_name": req.customer_name,
        "customer_site_id": req.customer_site_id,
        "created_by": req.created_by,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "updated_at": req.updated_at.isoformat() if req.updated_at else None,
        "deadline": req.deadline,
        "cloned_from_id": req.cloned_from_id,
        "last_searched_at": req.last_searched_at.isoformat() if req.last_searched_at else None,
        "requirement_count": len(req.requirements) if req.requirements else 0,
        "claimed_by_id": req.claimed_by_id,
        "claimed_at": req.claimed_at.isoformat() if req.claimed_at else None,
        "urgency": req.urgency or "normal",
        "opportunity_value": float(req.opportunity_value) if req.opportunity_value else None,
    }


@router.get("/api/requisitions/{req_id}/sourcing-score")
async def requisition_sourcing_score(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get detailed per-requirement sourcing scores for a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    from ...services.sourcing_score import compute_requisition_scores

    return compute_requisition_scores(req_id, db)


@router.post("/api/requisitions", response_model=RequisitionOut)
async def create_requisition(
    body: RequisitionCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from . import invalidate_prefix

    safe_name = _HTML_TAG_RE.sub("", body.name).strip() or "Untitled"
    safe_customer = _HTML_TAG_RE.sub("", body.customer_name).strip() if body.customer_name else body.customer_name
    req = Requisition(
        name=safe_name,
        customer_site_id=body.customer_site_id,
        customer_name=safe_customer,
        deadline=body.deadline,
        created_by=user.id,
        status="draft",
    )
    db.add(req)
    db.commit()
    invalidate_prefix("req_list")

    return {"id": req.id, "name": req.name}


@router.put("/api/requisitions/{req_id}/outcome")
async def mark_outcome(req_id: int, body: dict, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Mark a requisition as won or lost."""
    from . import invalidate_prefix

    outcome = (body.get("outcome") or "").lower()
    if outcome not in ("won", "lost"):
        raise HTTPException(400, "outcome must be 'won' or 'lost'")
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    req.status = outcome
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "status": req.status}


@router.put("/api/requisitions/{req_id}/archive")
async def toggle_archive(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from . import invalidate_prefix

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if req.status in ("archived", "won", "lost"):
        req.status = RequisitionStatus.ACTIVE
    else:
        req.status = RequisitionStatus.ARCHIVED
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "status": req.status}


@router.put("/api/requisitions/bulk-archive")
async def bulk_archive(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Archive all active requisitions NOT created by the current user."""
    from . import invalidate_prefix

    q = db.query(Requisition).filter(
        Requisition.created_by != user.id,
        Requisition.status.notin_(["archived", "won", "lost", "closed"]),
    )
    count = q.update({"status": "archived"}, synchronize_session="fetch")
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "archived_count": count}


@router.put("/api/requisitions/batch-archive")
async def batch_archive_by_ids(
    payload: BatchArchiveByIds,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive specific requisitions by ID list."""
    from . import invalidate_prefix

    q = db.query(Requisition).filter(
        Requisition.id.in_(payload.ids),
        Requisition.status.notin_(["archived", "won", "lost", "closed"]),
    )
    # Sales users can only archive their own requisitions
    if user.role == "sales":
        q = q.filter(Requisition.created_by == user.id)
    count = q.update({"status": "archived"}, synchronize_session="fetch")
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "archived_count": count}


@router.put("/api/requisitions/batch-assign")
async def batch_assign(
    payload: BatchAssign,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Assign owner to specific requisitions by ID list.

    Admin only.
    """
    from . import invalidate_prefix

    # Verify the target user exists
    target = db.query(User).filter(User.id == payload.owner_id).first()
    if not target:
        raise HTTPException(404, "Target user not found")
    count = (
        db.query(Requisition)
        .filter(Requisition.id.in_(payload.ids))
        .update({"claimed_by_id": payload.owner_id}, synchronize_session="fetch")
    )
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "assigned_count": count, "assigned_to": target.name or target.email}


@router.post("/api/requisitions/{req_id}/dismiss-new-offers")
async def dismiss_new_offers(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Mark offers as viewed so the flash alert stops."""
    from . import invalidate_prefix

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
    from . import invalidate_prefix

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if body.name is not None:
        req.name = _HTML_TAG_RE.sub("", body.name).strip() or req.name
    if body.customer_site_id is not None:
        req.customer_site_id = body.customer_site_id
    if body.deadline is not None:
        req.deadline = body.deadline or None
    if body.urgency is not None:
        if body.urgency not in ("normal", "hot", "critical"):
            raise HTTPException(400, "urgency must be normal, hot, or critical")
        req.urgency = body.urgency
    if body.opportunity_value is not None:
        req.opportunity_value = body.opportunity_value
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "name": req.name}


# ── Buyer Claim/Unclaim ─────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/claim")
async def claim_requisition_endpoint(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer claims a requisition for sourcing.

    Any unclaimed req is open to any buyer.
    """
    if user.role not in ("buyer", "trader", "manager", "admin"):
        raise HTTPException(403, "Only buyers can claim requisitions")
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    from ...services.requirement_status import claim_requisition
    from . import invalidate_prefix

    try:
        changed = claim_requisition(req, user, db)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "claimed": changed, "claimed_by_id": req.claimed_by_id}


@router.delete("/api/requisitions/{req_id}/claim")
async def unclaim_requisition_endpoint(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Release buyer's claim on a requisition.

    Only the claimer or admin can unclaim.
    """
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if req.claimed_by_id != user.id and user.role != "admin":
        raise HTTPException(403, "Only the claiming buyer or admin can unclaim")

    from ...services.requirement_status import unclaim_requisition
    from . import invalidate_prefix

    changed = unclaim_requisition(req, db, actor=user)
    db.commit()
    invalidate_prefix("req_list")
    return {"ok": True, "unclaimed": changed}
