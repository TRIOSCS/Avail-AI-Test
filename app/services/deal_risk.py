"""Deal risk predictor — scores requisition risk based on multiple signals.

Signals analyzed:
  - Days since last activity
  - Quote-to-close ratio for the customer
  - Vendor response rate
  - Price competitiveness (best offer vs target price)

Called by: app/routers/requisitions/core.py, app/jobs/notify_intelligence_jobs.py
Depends on: app/models/sourcing.py, app/models/offers.py, app/models/intelligence.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session


def assess_risk(requisition_id: int, db: Session) -> dict:
    """Compute risk assessment for a requisition.

    Returns {risk_level: green/yellow/red, score: 0-100,
             explanation: str, suggested_action: str, signals: dict}
    """
    from app.models.offers import Contact, Offer
    from app.models.quotes import Quote
    from app.models.sourcing import Requisition

    req = db.get(Requisition, requisition_id)
    if not req:
        return {"risk_level": "green", "score": 0, "explanation": "Requisition not found", "suggested_action": "", "signals": {}}

    now = datetime.now(timezone.utc)
    signals = {}
    risk_points = 0

    # Signal 1: Days since last activity (0-30 points)
    updated = req.updated_at
    if updated:
        updated = updated.replace(tzinfo=timezone.utc) if updated.tzinfo is None else updated
        days_idle = (now - updated).days
    else:
        days_idle = 0

    signals["days_idle"] = days_idle
    if days_idle >= 14:
        risk_points += 30
    elif days_idle >= 7:
        risk_points += 15
    elif days_idle >= 3:
        risk_points += 5

    # Signal 2: Offer coverage (0-25 points)
    from app.models.sourcing import Requirement
    req_count = db.query(func.count(Requirement.id)).filter(Requirement.requisition_id == requisition_id).scalar() or 1
    offer_count = db.query(func.count(Offer.id)).filter(Offer.requisition_id == requisition_id).scalar() or 0
    offer_ratio = offer_count / req_count if req_count else 0

    signals["offer_count"] = offer_count
    signals["requirement_count"] = req_count
    signals["offer_ratio"] = round(offer_ratio, 2)

    if offer_count == 0:
        risk_points += 25
    elif offer_ratio < 0.5:
        risk_points += 15
    elif offer_ratio < 1.0:
        risk_points += 5

    # Signal 3: Vendor response rate (0-20 points)
    contacts_sent = (
        db.query(func.count(Contact.id))
        .filter(Contact.requisition_id == requisition_id)
        .scalar()
    ) or 0

    signals["contacts_sent"] = contacts_sent
    if contacts_sent > 0:
        response_rate = offer_count / contacts_sent
        signals["vendor_response_rate"] = round(response_rate, 2)
        if response_rate < 0.1:
            risk_points += 20
        elif response_rate < 0.3:
            risk_points += 10
    elif offer_count == 0:
        risk_points += 15  # No outreach at all

    # Signal 4: Price competitiveness (0-15 points)
    best_offer = (
        db.query(func.min(Offer.unit_price))
        .filter(Offer.requisition_id == requisition_id, Offer.unit_price > 0)
        .scalar()
    )

    requirements = db.query(Requirement).filter(Requirement.requisition_id == requisition_id).all()
    if best_offer and requirements:
        target_prices = [r.target_price for r in requirements if r.target_price and r.target_price > 0]
        if target_prices:
            avg_target = sum(target_prices) / len(target_prices)
            price_gap = ((best_offer - avg_target) / avg_target) * 100 if avg_target else 0
            signals["price_gap_pct"] = round(price_gap, 1)
            if price_gap > 50:
                risk_points += 15
            elif price_gap > 20:
                risk_points += 8

    # Signal 5: Customer history (0-10 points)
    if req.customer_name:
        historical_wins = (
            db.query(func.count(Requisition.id))
            .filter(
                Requisition.customer_name == req.customer_name,
                Requisition.status == "won",
            )
            .scalar()
        ) or 0
        historical_losses = (
            db.query(func.count(Requisition.id))
            .filter(
                Requisition.customer_name == req.customer_name,
                Requisition.status == "lost",
            )
            .scalar()
        ) or 0

        signals["customer_wins"] = historical_wins
        signals["customer_losses"] = historical_losses
        total = historical_wins + historical_losses
        if total >= 3:
            win_rate = historical_wins / total
            signals["customer_win_rate"] = round(win_rate, 2)
            if win_rate < 0.3:
                risk_points += 10
            elif win_rate < 0.5:
                risk_points += 5

    # Compute final risk level
    risk_score = min(risk_points, 100)
    if risk_score >= 60:
        risk_level = "red"
    elif risk_score >= 30:
        risk_level = "yellow"
    else:
        risk_level = "green"

    # Generate explanation and action
    explanation, action = _generate_explanation(risk_level, signals, req)

    return {
        "risk_level": risk_level,
        "score": risk_score,
        "explanation": explanation,
        "suggested_action": action,
        "signals": signals,
    }


def _generate_explanation(risk_level: str, signals: dict, req) -> tuple[str, str]:
    """Generate human-readable explanation and action from signals."""
    issues = []
    actions = []

    days_idle = signals.get("days_idle", 0)
    if days_idle >= 7:
        issues.append(f"idle for {days_idle} days")
        actions.append("follow up with contacts")

    if signals.get("offer_count", 0) == 0:
        issues.append("no offers received")
        actions.append("send RFQs to more vendors")

    response_rate = signals.get("vendor_response_rate")
    if response_rate is not None and response_rate < 0.2:
        issues.append(f"low vendor response rate ({response_rate:.0%})")
        actions.append("try alternate vendors")

    price_gap = signals.get("price_gap_pct")
    if price_gap and price_gap > 20:
        issues.append(f"best price {price_gap:.0f}% above target")
        actions.append("negotiate pricing or find alternatives")

    win_rate = signals.get("customer_win_rate")
    if win_rate is not None and win_rate < 0.5:
        issues.append(f"customer win rate {win_rate:.0%}")

    if not issues:
        return "On track — no significant risk signals", ""

    explanation = f"Risk factors: {', '.join(issues)}"
    action = "; ".join(actions[:2]) if actions else ""
    return explanation, action


def scan_active_requisitions(db: Session, user_id: int | None = None) -> list[dict]:
    """Scan all active requisitions and return risk assessments.

    Optionally filter by user_id (creator).
    """
    from app.models.sourcing import Requisition

    query = db.query(Requisition).filter(
        Requisition.status.in_(["active", "sourcing", "offers"])
    )
    if user_id:
        query = query.filter(Requisition.created_by == user_id)

    results = []
    for req in query.all():
        try:
            risk = assess_risk(req.id, db)
            risk["requisition_id"] = req.id
            risk["requisition_name"] = req.name
            risk["customer_name"] = req.customer_name
            risk["created_by"] = req.created_by
            results.append(risk)
        except Exception:
            logger.debug("Risk assessment failed for req %d", req.id, exc_info=True)

    return results
