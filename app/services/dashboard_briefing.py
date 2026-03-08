"""Morning briefing service — role-aware daily summary for buyers and sales.

Aggregates actionable items from emails, knowledge entries, requisitions,
offers, and quotes into a structured briefing. Pure data aggregation with
NO AI calls.

Called by: routers/dashboard.py (future), jobs/ (future morning briefing job)
Depends on: models/email_intelligence.py, models/knowledge.py,
            models/sourcing.py, models/offers.py, models/quotes.py,
            services/activity_insights.py, services/deal_risk.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session


def generate_briefing(db: Session, user_id: int, role: str = "buyer") -> dict:
    """Build a morning briefing for the given user and role.

    Returns {"sections": [...], "total_items": int, "generated_at": str, "role": str}.
    Each section: {"name": str, "label": str, "count": int, "items": [...]}.
    Each item: {"title": str, "detail": str, "entity_type": str,
                "entity_id": int|str, "priority": str, "age_hours": float}.
    """
    now = datetime.now(timezone.utc)

    if role == "sales":
        sections = _build_sales_sections(db, user_id, now)
    else:
        sections = _build_buyer_sections(db, user_id, now)

    total = sum(s["count"] for s in sections)
    return {
        "sections": sections,
        "total_items": total,
        "generated_at": now.isoformat(),
        "role": role,
    }


# ---------------------------------------------------------------------------
# Buyer sections
# ---------------------------------------------------------------------------

def _build_buyer_sections(db: Session, user_id: int, now: datetime) -> list:
    return [
        _vendor_emails(db, user_id, now),
        _unanswered_questions(db, user_id, now),
        _stalling_deals(db, user_id, now),
        _resurfaced_parts(db, user_id, now),
        _price_movement(db, user_id, now),
    ]


def _vendor_emails(db: Session, user_id: int, now: datetime) -> dict:
    """Unreviewed offers/quotes from the last 24 hours."""
    try:
        from app.models.email_intelligence import EmailIntelligence

        cutoff = now - timedelta(hours=24)
        rows = (
            db.query(EmailIntelligence)
            .filter(
                EmailIntelligence.user_id == user_id,
                EmailIntelligence.needs_review.is_(True),
                EmailIntelligence.classification.in_(["offer", "quote_reply", "stock_list"]),
                EmailIntelligence.created_at >= cutoff,
            )
            .order_by(EmailIntelligence.created_at.desc())
            .all()
        )
        items = []
        for r in rows:
            age = (now - (r.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": "{} — {}".format(r.classification, r.sender_email),
                "detail": r.subject or "(no subject)",
                "entity_type": "email_intelligence",
                "entity_id": r.id,
                "priority": "high" if r.classification == "offer" else "medium",
                "age_hours": round(age, 1),
            })
        return _section("vendor_emails", "Vendor Emails to Review", items)
    except Exception:
        logger.debug("vendor_emails section failed", exc_info=True)
        return _section("vendor_emails", "Vendor Emails to Review", [])


def _unanswered_questions(db: Session, user_id: int, now: datetime) -> dict:
    """Knowledge questions assigned to user that are still open."""
    try:
        from app.models.knowledge import KnowledgeEntry

        rows = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.entry_type == "question",
                KnowledgeEntry.is_resolved.is_(False),
            )
            .all()
        )
        items = []
        for r in rows:
            assigned = r.assigned_to_ids or []
            if user_id not in assigned:
                continue
            age = (now - (r.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": (r.content or "")[:80],
                "detail": "Assigned, waiting for answer",
                "entity_type": "knowledge_entry",
                "entity_id": r.id,
                "priority": "high" if age > 48 else "medium",
                "age_hours": round(age, 1),
            })
        return _section("unanswered_questions", "Unanswered Questions", items)
    except Exception:
        logger.debug("unanswered_questions section failed", exc_info=True)
        return _section("unanswered_questions", "Unanswered Questions", [])


def _stalling_deals(db: Session, user_id: int, now: datetime) -> dict:
    """Requisitions owned by user with no new offer in 7+ days."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requisition

        cutoff = now - timedelta(days=7)
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            latest_offer = (
                db.query(func.max(Offer.created_at))
                .filter(Offer.requisition_id == req.id)
                .scalar()
            )
            if latest_offer is not None and latest_offer >= cutoff:
                continue
            age = (now - (req.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": req.name,
                "detail": "No new offers in 7+ days",
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "high",
                "age_hours": round(age, 1),
            })
        return _section("stalling_deals", "Stalling Deals", items)
    except Exception:
        logger.debug("stalling_deals section failed", exc_info=True)
        return _section("stalling_deals", "Stalling Deals", [])


def _resurfaced_parts(db: Session, user_id: int, now: datetime) -> dict:
    """New offers (last 24h) for MPNs from user's active requisitions."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        cutoff = now - timedelta(hours=24)
        # Get MPNs from active reqs owned by user
        active_mpns = (
            db.query(Requirement.mpn)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .distinct()
            .all()
        )
        mpn_set = {row[0] for row in active_mpns if row[0]}
        if not mpn_set:
            return _section("resurfaced_parts", "Resurfaced Parts", [])

        new_offers = (
            db.query(Offer)
            .filter(
                Offer.mpn.in_(list(mpn_set)),
                Offer.created_at >= cutoff,
            )
            .all()
        )
        items = []
        for o in new_offers:
            age = (now - (o.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": o.mpn,
                "detail": "New offer from {}".format(o.vendor_name),
                "entity_type": "offer",
                "entity_id": o.id,
                "priority": "medium",
                "age_hours": round(age, 1),
            })
        return _section("resurfaced_parts", "Resurfaced Parts", items)
    except Exception:
        logger.debug("resurfaced_parts section failed", exc_info=True)
        return _section("resurfaced_parts", "Resurfaced Parts", [])


def _price_movement(db: Session, user_id: int, now: datetime) -> dict:
    """MPNs where latest offer price differs >15% from median of recent 10."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        # Get active MPNs for user
        active_mpns = (
            db.query(Requirement.mpn)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .distinct()
            .all()
        )
        mpn_list = [row[0] for row in active_mpns if row[0]]
        if not mpn_list:
            return _section("price_movement", "Price Movement", [])

        items = []
        for mpn in mpn_list:
            recent = (
                db.query(Offer.unit_price)
                .filter(Offer.mpn == mpn, Offer.unit_price.isnot(None))
                .order_by(Offer.created_at.desc())
                .limit(10)
                .all()
            )
            prices = [float(r[0]) for r in recent if r[0]]
            if len(prices) < 2:
                continue
            latest = prices[0]
            sorted_prices = sorted(prices)
            mid = len(sorted_prices) // 2
            median = sorted_prices[mid] if len(sorted_prices) % 2 else (sorted_prices[mid - 1] + sorted_prices[mid]) / 2
            if median == 0:
                continue
            pct_diff = abs(latest - median) / median
            if pct_diff > 0.15:
                direction = "up" if latest > median else "down"
                items.append({
                    "title": mpn,
                    "detail": "Price moved {:.0%} {} (${:.2f} vs median ${:.2f})".format(pct_diff, direction, latest, median),
                    "entity_type": "mpn",
                    "entity_id": mpn,
                    "priority": "high" if pct_diff > 0.30 else "medium",
                    "age_hours": 0.0,
                })
        return _section("price_movement", "Price Movement", items)
    except Exception:
        logger.debug("price_movement section failed", exc_info=True)
        return _section("price_movement", "Price Movement", [])


# ---------------------------------------------------------------------------
# Sales sections
# ---------------------------------------------------------------------------

def _build_sales_sections(db: Session, user_id: int, now: datetime) -> list:
    return [
        _customer_followups(db, user_id, now),
        _new_answers(db, user_id, now),
        _quiet_customers(db, user_id, now),
        _deals_at_risk(db, user_id, now),
        _quotes_ready(db, user_id, now),
    ]


def _customer_followups(db: Session, user_id: int, now: datetime) -> dict:
    """Open requisitions owned by user not updated in 3+ days."""
    try:
        from app.models.sourcing import Requisition

        cutoff = now - timedelta(days=3)
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status == "open",
                Requisition.updated_at < cutoff,
            )
            .all()
        )
        items = []
        for req in reqs:
            age = (now - (req.updated_at or req.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": req.name,
                "detail": "No update in {:.0f}h".format(age),
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "high" if age > 168 else "medium",
                "age_hours": round(age, 1),
            })
        return _section("customer_followups", "Customer Follow-ups", items)
    except Exception:
        logger.debug("customer_followups section failed", exc_info=True)
        return _section("customer_followups", "Customer Follow-ups", [])


def _new_answers(db: Session, user_id: int, now: datetime) -> dict:
    """Knowledge answers created in the last 24 hours."""
    try:
        from app.models.knowledge import KnowledgeEntry

        cutoff = now - timedelta(hours=24)
        rows = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.entry_type == "answer",
                KnowledgeEntry.created_at >= cutoff,
            )
            .all()
        )
        items = []
        for r in rows:
            age = (now - (r.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": (r.content or "")[:80],
                "detail": "New answer from {}".format(r.source),
                "entity_type": "knowledge_entry",
                "entity_id": r.id,
                "priority": "low",
                "age_hours": round(age, 1),
            })
        return _section("new_answers", "New Answers", items)
    except Exception:
        logger.debug("new_answers section failed", exc_info=True)
        return _section("new_answers", "New Answers", [])


def _quiet_customers(db: Session, user_id: int, now: datetime) -> dict:
    """Delegates to activity_insights._detect_gone_quiet."""
    try:
        from app.services.activity_insights import _detect_gone_quiet

        insights = _detect_gone_quiet(user_id, db)
        items = []
        for ins in insights:
            items.append({
                "title": ins.get("title", "Quiet customer"),
                "detail": ins.get("detail", ""),
                "entity_type": "company",
                "entity_id": ins.get("entity_id", 0),
                "priority": ins.get("priority", "medium"),
                "age_hours": 0.0,
            })
        return _section("quiet_customers", "Quiet Customers", items)
    except Exception:
        logger.debug("quiet_customers section failed", exc_info=True)
        return _section("quiet_customers", "Quiet Customers", [])


def _deals_at_risk(db: Session, user_id: int, now: datetime) -> dict:
    """Requisitions owned by user where deal_risk returns risk_level=red."""
    try:
        from app.models.sourcing import Requisition
        from app.services.deal_risk import assess_risk

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            try:
                risk = assess_risk(req.id, db)
            except Exception:
                continue
            if risk.get("risk_level") != "red":
                continue
            age = (now - (req.created_at or now)).total_seconds() / 3600.0
            items.append({
                "title": req.name,
                "detail": risk.get("explanation", "High risk"),
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "high",
                "age_hours": round(age, 1),
            })
        return _section("deals_at_risk", "Deals at Risk", items)
    except Exception:
        logger.debug("deals_at_risk section failed", exc_info=True)
        return _section("deals_at_risk", "Deals at Risk", [])


def _quotes_ready(db: Session, user_id: int, now: datetime) -> dict:
    """Requisitions with more offers than quotes — ready to quote."""
    try:
        from app.models.offers import Offer
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            offer_count = db.query(func.count(Offer.id)).filter(Offer.requisition_id == req.id).scalar() or 0
            quote_count = db.query(func.count(Quote.id)).filter(Quote.requisition_id == req.id).scalar() or 0
            if offer_count > quote_count:
                age = (now - (req.created_at or now)).total_seconds() / 3600.0
                items.append({
                    "title": req.name,
                    "detail": "{} offers, {} quotes — ready to quote".format(offer_count, quote_count),
                    "entity_type": "requisition",
                    "entity_id": req.id,
                    "priority": "medium",
                    "age_hours": round(age, 1),
                })
        return _section("quotes_ready", "Quotes Ready", items)
    except Exception:
        logger.debug("quotes_ready section failed", exc_info=True)
        return _section("quotes_ready", "Quotes Ready", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _section(name: str, label: str, items: list) -> dict:
    """Build a standard section dict."""
    return {
        "name": name,
        "label": label,
        "count": len(items),
        "items": items,
    }
