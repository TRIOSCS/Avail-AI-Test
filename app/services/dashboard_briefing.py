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

    if role == "director":
        sections = _build_director_sections(db, user_id, now)
    elif role == "sales":
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
        _open_rfqs_no_offers(db, user_id, now),
        _vendor_emails(db, user_id, now),
        _unanswered_questions(db, user_id, now),
        _stalling_deals(db, user_id, now),
        _resurfaced_parts(db, user_id, now),
        _price_movement(db, user_id, now),
    ]


def _open_rfqs_no_offers(db: Session, user_id: int, now: datetime) -> dict:
    """Open requisitions with zero confirmed offers — awaiting sourcing."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requisition

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.created_by == user_id,
                Requisition.status.in_(["open", "in_progress", "active"]),
            )
            .all()
        )
        items = []
        for req in reqs:
            offer_count = (
                db.query(func.count(Offer.id))
                .filter(Offer.requisition_id == req.id)
                .scalar()
            ) or 0
            if offer_count > 0:
                continue
            age = (now - (req.created_at or now)).total_seconds() / 3600.0
            days = age / 24.0
            items.append({
                "title": "{} — {}".format(
                    req.name or "Req #{}".format(req.id),
                    req.customer_name or "unknown customer",
                ),
                "detail": "No offers yet — entered {:.0f}d ago".format(days),
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "high" if days > 3 else "medium",
                "age_hours": round(age, 1),
            })
        return _section("open_rfqs_no_offers", "Open RFQs Awaiting Sourcing", items)
    except Exception:
        logger.debug("open_rfqs_no_offers section failed", exc_info=True)
        return _section("open_rfqs_no_offers", "Open RFQs Awaiting Sourcing", [])


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
        _quotes_needing_followup(db, user_id, now),
        _overnight_vendor_quotes(db, user_id, now),
        _customer_followups(db, user_id, now),
        _new_answers(db, user_id, now),
        _quiet_customers(db, user_id, now),
        _deals_at_risk(db, user_id, now),
        _quotes_ready(db, user_id, now),
    ]


def _quotes_needing_followup(db: Session, user_id: int, now: datetime) -> dict:
    """Quotes sent >48h ago with no customer response — needs follow-up."""
    try:
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        cutoff_48h = now - timedelta(hours=48)
        quotes = (
            db.query(Quote)
            .join(Requisition, Quote.requisition_id == Requisition.id)
            .filter(
                Requisition.created_by == user_id,
                Quote.sent_at.isnot(None),
                Quote.sent_at < cutoff_48h,
                Quote.followup_alert_sent_at.is_(None),
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .all()
        )
        items = []
        for q in quotes:
            age = (now - (q.sent_at or now)).total_seconds() / 3600.0
            req = db.get(Requisition, q.requisition_id)
            customer = req.customer_name if req else "unknown"
            items.append({
                "title": "{} — quoted {:.0f}h ago".format(customer, age),
                "detail": "No response since quote sent",
                "entity_type": "quote",
                "entity_id": q.id,
                "priority": "high" if age > 96 else "medium",
                "age_hours": round(age, 1),
            })
            # Mark followup sent so we only alert once
            q.followup_alert_sent_at = now

        if items:
            try:
                db.commit()
            except Exception:
                db.rollback()

        return _section("quotes_needing_followup", "Quotes Needing Follow-up", items)
    except Exception:
        logger.debug("quotes_needing_followup section failed", exc_info=True)
        return _section("quotes_needing_followup", "Quotes Needing Follow-up", [])


def _overnight_vendor_quotes(db: Session, user_id: int, now: datetime) -> dict:
    """Vendor offers received since yesterday on this AM's requisitions."""
    try:
        from app.models.offers import Offer
        from app.models.sourcing import Requisition

        cutoff = now - timedelta(hours=24)
        offers = (
            db.query(Offer)
            .join(Requisition, Offer.requisition_id == Requisition.id)
            .filter(
                Requisition.created_by == user_id,
                Offer.created_at >= cutoff,
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
            )
            .order_by(Offer.created_at.desc())
            .limit(20)
            .all()
        )
        items = []
        for o in offers:
            price_str = "${:.2f}".format(float(o.unit_price)) if o.unit_price else "no price"
            items.append({
                "title": "{} quoted {} on {}".format(
                    o.vendor_name or "Unknown vendor", price_str, o.mpn or "unknown MPN"
                ),
                "detail": "Req #{}".format(o.requisition_id),
                "entity_type": "offer",
                "entity_id": o.id,
                "priority": "medium",
                "age_hours": round((now - (o.created_at or now)).total_seconds() / 3600.0, 1),
            })
        return _section("overnight_vendor_quotes", "Overnight Vendor Quotes", items)
    except Exception:
        logger.debug("overnight_vendor_quotes section failed", exc_info=True)
        return _section("overnight_vendor_quotes", "Overnight Vendor Quotes", [])


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
# Director sections
# ---------------------------------------------------------------------------

def _build_director_sections(db: Session, user_id: int, now: datetime) -> list:
    return [
        _high_value_idle_deals(db, now),
        _team_response_times(db, now),
        _workload_snapshot(db, now),
        _stale_accounts(db, now),
        _avail_scores_summary(db, now),
    ]


def _high_value_idle_deals(db: Session, now: datetime) -> dict:
    """High-value deals (by target_qty * target_price) idle >48h."""
    try:
        from app.models.sourcing import Requirement, Requisition

        cutoff = now - timedelta(hours=48)
        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.status.in_(["open", "in_progress", "quoting", "active"]),
                Requisition.updated_at < cutoff,
            )
            .all()
        )
        items = []
        for req in reqs:
            # Estimate value from requirements
            parts = (
                db.query(Requirement)
                .filter(Requirement.requisition_id == req.id)
                .all()
            )
            total_value = 0.0
            for p in parts:
                qty = float(p.target_qty or 0)
                price = float(p.target_price or 0)
                total_value += qty * price
            if total_value < 100:  # Skip low-value deals
                continue
            idle_hours = (now - (req.updated_at or req.created_at or now)).total_seconds() / 3600.0
            try:
                from app.models.auth import User
                owner = db.get(User, req.created_by)
            except Exception:
                owner = None
            owner_name = owner.name if owner and hasattr(owner, "name") else "Unknown"
            items.append({
                "title": "{} — ${:,.0f}".format(req.name or "Req #{}".format(req.id), total_value),
                "detail": "Idle {:.0f}h — owned by {}".format(idle_hours, owner_name),
                "entity_type": "requisition",
                "entity_id": req.id,
                "priority": "high" if idle_hours > 96 else "medium",
                "age_hours": round(idle_hours, 1),
            })
        items.sort(key=lambda x: -x["age_hours"])
        return _section("high_value_idle_deals", "High-Value Deals Idle >48h", items[:15])
    except Exception:
        logger.debug("high_value_idle_deals section failed", exc_info=True)
        return _section("high_value_idle_deals", "High-Value Deals Idle >48h", [])


def _team_response_times(db: Session, now: datetime) -> dict:
    """Average response time per AM over the last 7 days (quote sent_at - req created_at)."""
    try:
        from app.models.auth import User
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        cutoff = now - timedelta(days=7)
        rows = (
            db.query(
                Requisition.created_by,
                func.avg(
                    func.extract("epoch", Quote.sent_at) - func.extract("epoch", Requisition.created_at)
                ),
                func.count(Quote.id),
            )
            .join(Quote, Quote.requisition_id == Requisition.id)
            .filter(Quote.sent_at >= cutoff, Quote.sent_at.isnot(None))
            .group_by(Requisition.created_by)
            .all()
        )
        items = []
        for user_id_val, avg_seconds, quote_count in rows:
            if avg_seconds is None:
                continue
            avg_hours = float(avg_seconds) / 3600.0
            try:
                user = db.get(User, user_id_val)
                name = user.name if user and hasattr(user, "name") else "User #{}".format(user_id_val)
            except Exception:
                name = "User #{}".format(user_id_val)
            items.append({
                "title": "{} — avg {:.1f}h".format(name, avg_hours),
                "detail": "{} quotes sent this week".format(quote_count),
                "entity_type": "user",
                "entity_id": user_id_val,
                "priority": "high" if avg_hours > 48 else "medium" if avg_hours > 24 else "low",
                "age_hours": round(avg_hours, 1),
            })
        items.sort(key=lambda x: -x["age_hours"])
        return _section("team_response_times", "Team Response Times (7d Avg)", items)
    except Exception:
        logger.debug("team_response_times section failed", exc_info=True)
        return _section("team_response_times", "Team Response Times (7d Avg)", [])


def _workload_snapshot(db: Session, now: datetime) -> dict:
    """Active requisitions per AM — shows workload distribution."""
    try:
        from app.models.auth import User
        from app.models.sourcing import Requisition

        rows = (
            db.query(Requisition.created_by, func.count(Requisition.id))
            .filter(Requisition.status.in_(["open", "in_progress", "quoting", "active"]))
            .group_by(Requisition.created_by)
            .all()
        )
        items = []
        for user_id_val, req_count in rows:
            try:
                user = db.get(User, user_id_val)
                name = user.name if user and hasattr(user, "name") else "User #{}".format(user_id_val)
            except Exception:
                name = "User #{}".format(user_id_val)
            items.append({
                "title": "{} — {} active reqs".format(name, req_count),
                "detail": "Currently active requisitions",
                "entity_type": "user",
                "entity_id": user_id_val,
                "priority": "high" if req_count > 20 else "medium" if req_count > 10 else "low",
                "age_hours": 0.0,
            })
        items.sort(key=lambda x: -int(x["title"].split(" — ")[1].split(" ")[0]))
        return _section("workload_snapshot", "Workload per AM", items)
    except Exception:
        logger.debug("workload_snapshot section failed", exc_info=True)
        return _section("workload_snapshot", "Workload per AM", [])


def _stale_accounts(db: Session, now: datetime) -> dict:
    """Companies with no activity in 5+ days."""
    try:
        from app.models.crm import Company

        cutoff = now - timedelta(days=5)
        companies = (
            db.query(Company)
            .filter(
                Company.is_active.is_(True),
                Company.last_activity_at.isnot(None),
                Company.last_activity_at < cutoff,
            )
            .order_by(Company.last_activity_at.asc())
            .limit(20)
            .all()
        )
        items = []
        for c in companies:
            idle_days = (now - c.last_activity_at).total_seconds() / 86400.0
            # Get owner name
            owner_name = "Unassigned"
            if c.account_owner_id:
                try:
                    from app.models.auth import User
                    owner = db.get(User, c.account_owner_id)
                    owner_name = owner.name if owner and hasattr(owner, "name") else "Unknown"
                except Exception:
                    pass
            items.append({
                "title": "{} — {:.0f}d idle".format(c.name, idle_days),
                "detail": "Owned by {}".format(owner_name),
                "entity_type": "company",
                "entity_id": c.id,
                "priority": "high" if idle_days > 14 else "medium",
                "age_hours": round(idle_days * 24, 1),
            })
        return _section("stale_accounts", "Stale Accounts (5+ Days)", items)
    except Exception:
        logger.debug("stale_accounts section failed", exc_info=True)
        return _section("stale_accounts", "Stale Accounts (5+ Days)", [])


def _avail_scores_summary(db: Session, now: datetime) -> dict:
    """Top/bottom Avail Scores from the latest month — conditional on data."""
    try:
        from app.models.performance import AvailScoreSnapshot

        # Get the latest month with data
        latest_month = (
            db.query(func.max(AvailScoreSnapshot.month))
            .scalar()
        )
        if not latest_month:
            return _section("avail_scores", "Avail Scores", [])

        snapshots = (
            db.query(AvailScoreSnapshot)
            .filter(AvailScoreSnapshot.month == latest_month)
            .order_by(AvailScoreSnapshot.total_score.desc())
            .all()
        )
        items = []
        for s in snapshots:
            try:
                from app.models.auth import User
                user = db.get(User, s.user_id)
                name = user.name if user and hasattr(user, "name") else "User #{}".format(s.user_id)
            except Exception:
                name = "User #{}".format(s.user_id)
            rank_label = "#{}".format(s.rank) if hasattr(s, "rank") and s.rank else ""
            items.append({
                "title": "{} {} — {:.0f} pts".format(rank_label, name, float(s.total_score or 0)),
                "detail": "{} role".format(s.role_type) if hasattr(s, "role_type") else "",
                "entity_type": "user",
                "entity_id": s.user_id,
                "priority": "low",
                "age_hours": 0.0,
            })
        return _section("avail_scores", "Avail Scores", items)
    except Exception:
        logger.debug("avail_scores section failed", exc_info=True)
        return _section("avail_scores", "Avail Scores", [])


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
