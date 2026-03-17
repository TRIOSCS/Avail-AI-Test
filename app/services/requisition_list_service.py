"""Service for querying the requisitions list with filters, search, sort, pagination.

Reuses the proven 22-subquery pattern from app/routers/requisitions/core.py
but exposed as a reusable service for the requisitions views.

Called by: app/routers/htmx_views.py
Depends on: app/models/sourcing.py, app/models/offers.py, SQLAlchemy
"""

from typing import Any

from sqlalchemy import and_, case, exists, literal, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from app.models import (
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
from app.schemas.requisitions2 import PaginationContext, ReqListFilters
from app.utils.sql_helpers import escape_like


def _compute_sourcing_score(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt):
    """Lightweight sourcing score for list views."""
    from app.services.sourcing_score import compute_requisition_score_fast

    return compute_requisition_score_fast(
        req_count=req_cnt or 0,
        sourced_count=sourced_cnt or 0,
        rfq_sent_count=rfq_sent or 0,
        reply_count=reply_cnt or 0,
        offer_count=offer_cnt or 0,
        call_count=call_cnt or 0,
        email_count=email_act_cnt or 0,
    )


def _build_pagination(page: int, per_page: int, total: int) -> PaginationContext:
    """Build pagination context from query results."""
    return PaginationContext(
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
    )


def list_requisitions(
    db: Session,
    filters: ReqListFilters,
    user_id: int,
    user_role: str,
) -> dict[str, Any]:
    """Fetch filtered, sorted, paginated requisition list.

    Returns dict with keys:
        requisitions: list[dict]  — enriched requisition rows
        pagination: PaginationContext
        filters: ReqListFilters  — echo back for template rendering
    """
    # ── Correlated subqueries (same pattern as core.py) ──────────────
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
        .where(Contact.requisition_id == Requisition.id, Contact.status == "sent")
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

    # ── Build query ──────────────────────────────────────────────────
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

    # ── Role-based filtering ─────────────────────────────────────────
    if user_role == "sales":
        query = query.filter(Requisition.created_by == user_id)

    # ── Search ───────────────────────────────────────────────────────
    q = filters.q.strip()
    if q:
        safe_q = escape_like(q)
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
    # ── Status filter ────────────────────────────────────────────────
    elif filters.status.value == "all":
        pass  # no status filter
    elif filters.status.value == "archived":
        query = query.filter(Requisition.status.in_(["archived", "won", "lost", "closed"]))
    else:
        query = query.filter(Requisition.status == filters.status.value)

    # ── Owner filter ─────────────────────────────────────────────────
    if filters.owner:
        query = query.filter(Requisition.created_by == filters.owner)

    # ── Urgency filter ───────────────────────────────────────────────
    if filters.urgency:
        query = query.filter(Requisition.urgency == filters.urgency.value)

    # ── Date range filter ────────────────────────────────────────────
    if filters.date_from:
        query = query.filter(Requisition.created_at >= filters.date_from)
    if filters.date_to:
        query = query.filter(Requisition.created_at <= filters.date_to)

    # ── Sort ─────────────────────────────────────────────────────────
    allowed_sorts = {
        "created_at": Requisition.created_at,
        "name": Requisition.name,
        "status": Requisition.status,
        "customer_name": Requisition.customer_name,
        "deadline": Requisition.deadline,
        "updated_at": Requisition.updated_at,
    }
    sort_col = allowed_sorts.get(filters.sort.value, Requisition.created_at)
    sort_expr = sort_col.asc() if filters.order == filters.order.asc else sort_col.desc()

    # ── Count + paginate ─────────────────────────────────────────────
    total = query.count()
    offset = (filters.page - 1) * filters.per_page
    rows = query.order_by(sort_expr).offset(offset).limit(filters.per_page).all()

    # ── Resolve creator names ────────────────────────────────────────
    creator_names = {}
    creator_ids = {r.created_by for r, *_ in rows if r.created_by}
    if creator_ids:
        creators = db.query(User.id, User.name, User.email).filter(User.id.in_(creator_ids)).all()
        creator_names = {u.id: u.name or u.email.split("@")[0] for u in creators}

    # ── Build response dicts ─────────────────────────────────────────
    requisitions = []
    for (
        r,
        req_cnt,
        con_cnt,
        reply_cnt,
        latest_reply,
        has_new,
        latest_offer,
        sourced_cnt,
        rfq_sent,
        needs_rev,
        ttv,
        q_status,
        q_sent,
        q_total,
        q_won,
        offer_cnt,
        best_price,
        await_cnt,
        pm_cnt,
        call_cnt,
        email_act_cnt,
        latest_rfq_sent,
    ) in rows:
        _sc, _sc_color, _sc_signals = _compute_sourcing_score(
            req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt
        )
        requisitions.append(
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
                "requirement_count": req_cnt or 0,
                "contact_count": con_cnt or 0,
                "reply_count": reply_cnt or 0,
                "latest_reply_at": latest_reply,
                "has_new_offers": bool(has_new),
                "latest_offer_at": latest_offer,
                "created_by": r.created_by,
                "created_by_name": creator_names.get(r.created_by, ""),
                "created_at": r.created_at,
                "last_searched_at": r.last_searched_at,
                "sourced_count": sourced_cnt or 0,
                "rfq_sent_count": rfq_sent or 0,
                "latest_rfq_sent_at": latest_rfq_sent,
                "cloned_from_id": r.cloned_from_id,
                "deadline": r.deadline,
                "needs_review_count": needs_rev or 0,
                "total_target_value": float(ttv or 0),
                "quote_status": q_status,
                "quote_sent_at": q_sent,
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
        )

    pagination = _build_pagination(filters.page, filters.per_page, total)

    return {
        "requisitions": requisitions,
        "pagination": pagination,
        "filters": filters,
    }


def get_requisition_detail(
    db: Session,
    req_id: int,
    user_id: int,
    user_role: str,
) -> dict[str, Any] | None:
    """Fetch single requisition with requirements for modal display.

    Returns None if not found or not accessible.
    """
    query = db.query(Requisition).filter(Requisition.id == req_id)
    if user_role == "sales":
        query = query.filter(Requisition.created_by == user_id)
    req = query.first()
    if not req:
        return None

    # Load customer display name
    customer_display = req.customer_name or ""
    if req.customer_site_id:
        site = db.query(CustomerSite).filter(CustomerSite.id == req.customer_site_id).first()
        if site and site.company:
            customer_display = f"{site.company.name} — {site.site_name}"

    # Creator name
    creator_name = ""
    if req.created_by:
        creator = db.query(User.name, User.email).filter(User.id == req.created_by).first()
        if creator:
            creator_name = creator.name or creator.email.split("@")[0]

    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).order_by(Requirement.id).all()

    return {
        "req": {
            "id": req.id,
            "name": req.name,
            "status": req.status,
            "customer_display": customer_display,
            "created_by_name": creator_name,
            "requirement_count": len(requirements),
            "offer_count": 0,  # Lightweight — no subquery here
            "urgency": req.urgency or "normal",
            "deadline": req.deadline,
            "created_at": req.created_at,
            "claimed_by_id": req.claimed_by_id,
        },
        "requirements": requirements,
    }


def get_row_context(db: Session, req: Requisition, user) -> dict:
    """Build template context for a single row after inline edit.

    Called by: app/routers/htmx_views.py (inline_save)
    Depends on: Requisition model, User model
    """
    # Requirement count
    req_cnt = db.query(sqlfunc.count(Requirement.id)).filter(Requirement.requisition_id == req.id).scalar() or 0
    # Offer count
    offer_cnt = db.query(sqlfunc.count(Offer.id)).filter(Offer.requisition_id == req.id).scalar() or 0
    # Creator name
    creator = db.query(User).filter(User.id == req.created_by).first()
    creator_name = creator.name or creator.email if creator else ""
    # Customer display
    customer_display = req.customer_name or ""
    if req.customer_site_id:
        site = db.query(CustomerSite).filter(CustomerSite.id == req.customer_site_id).first()
        if site and site.company:
            customer_display = f"{site.company.name} — {site.site_name}"
    return {
        "req": {
            "id": req.id,
            "name": req.name,
            "status": req.status,
            "customer_display": customer_display,
            "requirement_count": req_cnt,
            "offer_count": offer_cnt,
            "created_by": req.created_by,
            "created_by_name": creator_name,
            "created_at": req.created_at,
            "claimed_by_id": req.claimed_by_id,
            "urgency": req.urgency or "normal",
        },
        "user": user,
    }


def get_team_users(db: Session) -> list[dict]:
    """Get list of active users for owner filter dropdown."""
    users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    return [{"id": u.id, "display_name": u.name or u.email} for u in users]
